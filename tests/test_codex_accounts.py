from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest

import codexbot.codex_accounts as codex_accounts
from codexbot.codex_accounts import CodexAccountError, CodexAccountManager


def _id_token(email: str, account_id: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                    "chatgpt_plan_type": "plus",
                },
            }
        ).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{payload}.signature"


def _chatgpt_account(
    account_id: str,
    email: str,
    *,
    auth_state: str = "ready",
) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": email,
        "email": email,
        "plan_type": "plus",
        "auth_mode": "chat_g_p_t",
        "auth_data": {
            "type": "chat_g_p_t",
            "id_token": _id_token(email, account_id),
            "access_token": f"access-{account_id}",
            "refresh_token": f"refresh-{account_id}",
            "account_id": account_id,
        },
        "auth_state": auth_state,
        "last_used_at": None,
    }


def _api_key_account(account_id: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": "API account",
        "email": None,
        "plan_type": None,
        "auth_mode": "api_key",
        "auth_data": {"type": "api_key", "key": "sk-test-not-real"},
        "auth_state": "ready",
        "last_used_at": None,
    }


def _write_store(
    switcher_home: Path,
    *,
    second_state: str = "ready",
    include_api_key: bool = False,
) -> None:
    switcher_home.mkdir(parents=True)
    accounts = [
        _chatgpt_account("account-one", "one@example.com"),
        _chatgpt_account("account-two", "two@example.com", auth_state=second_state),
    ]
    if include_api_key:
        accounts.append(_api_key_account("account-api"))
    (switcher_home / "accounts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": accounts,
                "active_account_id": "account-one",
                "masked_account_ids": [],
            }
        ),
        encoding="utf-8",
    )


def _manager(
    tmp_path: Path,
    *,
    process_checker=lambda: (),  # type: ignore[no-untyped-def]
) -> CodexAccountManager:
    return CodexAccountManager(
        switcher_home=tmp_path / ".codex-switcher",
        codex_home=tmp_path / ".codex",
        process_checker=process_checker,
    )


def test_paths_honor_switcher_and_codex_home_environment(
    monkeypatch, tmp_path: Path
) -> None:
    switcher_home = tmp_path / "switcher-override"
    codex_home = tmp_path / "codex-override"
    monkeypatch.setenv("CODEX_SWITCHER_HOME", str(switcher_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    manager = CodexAccountManager(process_checker=lambda: ())

    assert manager.accounts_path == switcher_home / "accounts.json"
    assert manager.auth_path == codex_home / "auth.json"


def test_load_and_resolve_by_index_name_email_and_id_prefix(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher")
    manager = _manager(tmp_path)

    store = manager.load_store()

    assert [account.id for account in store.accounts] == ["account-one", "account-two"]
    assert store.active_account_id == "account-one"
    assert manager.resolve_account("2").id == "account-two"
    assert manager.resolve_account("two@example.com").id == "account-two"
    assert manager.resolve_account("account-two").id == "account-two"
    assert manager.resolve_account("account-t").id == "account-two"
    assert "access-account-one" not in repr(store.accounts[0])
    assert "refresh-account-one" not in repr(store.accounts[0].tokens)


def test_switch_writes_auth_and_updates_active_metadata(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = _manager(tmp_path)

    account = manager.switch_account("2")

    auth = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    store = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    assert account.id == "account-two"
    assert "auth_mode" not in auth
    assert auth["tokens"]["access_token"] == "access-account-two"
    assert auth["tokens"]["account_id"] == "account-two"
    assert store["active_account_id"] == "account-two"
    assert store["accounts"][1]["last_used_at"]


def test_switch_reconciles_rotated_tokens_before_switching_away(tmp_path: Path) -> None:
    """A refresh performed by Codex must be retained for the next switch."""

    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = _manager(tmp_path)
    manager.auth_path.parent.mkdir(parents=True)

    rotated_id_token = _id_token("one@example.com", "account-one")
    manager.auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": rotated_id_token,
                    "access_token": "access-account-one-rotated",
                    "refresh_token": "refresh-account-one-rotated",
                    "account_id": "account-one",
                },
                "last_refresh": "2026-08-04T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    switched = manager.switch_account("2")

    root = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    first_auth = root["accounts"][0]["auth_data"]
    assert first_auth["id_token"] == rotated_id_token
    assert first_auth["access_token"] == "access-account-one-rotated"
    assert first_auth["refresh_token"] == "refresh-account-one-rotated"
    assert switched.id == "account-two"

    # The target account is written from the reconciled catalogue, while the
    # old account's newly rotated refresh token remains available on return.
    target_auth = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    assert target_auth["tokens"]["refresh_token"] == "refresh-account-two"

    manager.switch_account("1")
    returned_auth = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    assert returned_auth["tokens"]["refresh_token"] == "refresh-account-one-rotated"


def test_reconcile_does_not_assign_rotated_tokens_to_wrong_account(tmp_path: Path) -> None:
    """A manually replaced auth.json must not poison the active account."""

    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = _manager(tmp_path)
    manager.auth_path.parent.mkdir(parents=True)
    manager.auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _id_token("two@example.com", "account-two"),
                    "access_token": "access-account-two-unrelated",
                    "refresh_token": "refresh-account-two-unrelated",
                    "account_id": "account-two",
                },
            }
        ),
        encoding="utf-8",
    )

    manager.reconcile_active_account()

    root = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    first_auth = root["accounts"][0]["auth_data"]
    assert first_auth["access_token"] == "access-account-one"
    assert first_auth["refresh_token"] == "refresh-account-one"


def test_reconcile_uses_token_account_id_when_accounts_share_an_email(
    tmp_path: Path,
) -> None:
    """Two workspaces can share an email but must not share refreshed tokens."""

    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    root = json.loads((switcher_home / "accounts.json").read_text(encoding="utf-8"))
    root["accounts"][1]["name"] = "one@example.com"
    root["accounts"][1]["email"] = "one@example.com"
    root["accounts"][1]["auth_data"]["id_token"] = _id_token(
        "one@example.com", "account-two"
    )
    (switcher_home / "accounts.json").write_text(json.dumps(root), encoding="utf-8")

    manager = _manager(tmp_path)
    manager.auth_path.parent.mkdir(parents=True)
    manager.auth_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "id_token": _id_token("one@example.com", "account-two"),
                    "access_token": "access-account-two-unrelated",
                    "refresh_token": "refresh-account-two-unrelated",
                }
            }
        ),
        encoding="utf-8",
    )

    manager.reconcile_active_account()

    saved = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    first_auth = saved["accounts"][0]["auth_data"]
    assert first_auth["access_token"] == "access-account-one"
    assert first_auth["refresh_token"] == "refresh-account-one"


def test_switch_supports_api_key_accounts(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher", include_api_key=True)
    manager = _manager(tmp_path)

    account = manager.switch_account("3")
    auth = json.loads(manager.auth_path.read_text(encoding="utf-8"))

    assert account.auth_type == "api_key"
    assert auth == {"OPENAI_API_KEY": "sk-test-not-real"}


def test_switch_is_blocked_while_codex_is_running(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher")
    manager = _manager(tmp_path, process_checker=lambda: (1234, 5678))

    with pytest.raises(CodexAccountError, match="正在运行"):
        manager.switch_account("2")

    assert not manager.auth_path.exists()
    assert manager.load_store().active_account_id == "account-one"


def test_switch_rechecks_processes_immediately_before_auth_write(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher")
    checks = iter(((), (4321,)))
    manager = _manager(tmp_path, process_checker=lambda: next(checks))

    with pytest.raises(CodexAccountError, match="正在运行"):
        manager.switch_account("2")

    assert not manager.auth_path.exists()
    assert manager.load_store().active_account_id == "account-one"


def test_expired_account_is_rejected_before_auth_write(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher", second_state="reauth_required")
    manager = _manager(tmp_path)

    with pytest.raises(CodexAccountError, match="登录已过期"):
        manager.switch_account("2")

    assert not manager.auth_path.exists()


def test_unknown_auth_state_is_rejected_before_auth_write(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher", second_state="disabled")
    manager = _manager(tmp_path)

    with pytest.raises(CodexAccountError, match="登录已过期"):
        manager.switch_account("2")

    assert not manager.auth_path.exists()


def test_switch_rolls_auth_back_when_store_update_fails(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher")
    manager = _manager(tmp_path)
    manager.auth_path.parent.mkdir(parents=True)
    original = b'{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}\n'
    manager.auth_path.write_bytes(original)

    def fail_mark(_account_id: str) -> None:
        raise CodexAccountError("simulated store failure")

    manager._mark_active = fail_mark  # type: ignore[method-assign]
    with pytest.raises(CodexAccountError, match="simulated store failure"):
        manager.switch_account("2")

    assert manager.auth_path.read_bytes() == original


def test_concurrent_switches_are_serialized_and_leave_consistent_state(tmp_path: Path) -> None:
    _write_store(tmp_path / ".codex-switcher")
    manager = _manager(tmp_path)
    original_write = manager._write_auth
    state_lock = threading.Lock()
    active_writers = 0
    max_active_writers = 0

    def tracked_write(account) -> None:  # type: ignore[no-untyped-def]
        nonlocal active_writers, max_active_writers
        with state_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        try:
            time.sleep(0.03)
            original_write(account)
        finally:
            with state_lock:
                active_writers -= 1

    manager._write_auth = tracked_write  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(manager.switch_account, selector) for selector in ("1", "2")]
        assert {future.result().id for future in results} == {"account-one", "account-two"}

    auth = json.loads(manager.auth_path.read_text(encoding="utf-8"))
    store = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    assert max_active_writers == 1
    assert auth["tokens"]["account_id"] == store["active_account_id"]


def test_store_update_retries_without_losing_a_concurrent_gui_change(
    monkeypatch, tmp_path: Path
) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = _manager(tmp_path)
    real_atomic_write = codex_accounts._atomic_write_json
    calls = 0

    def racing_atomic_write(path, value, *, expected_content=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        if path != manager.accounts_path:
            return real_atomic_write(
                path,
                value,
                expected_content=expected_content,
            )
        calls += 1
        if calls == 1:
            concurrent = json.loads(path.read_text(encoding="utf-8"))
            concurrent["masked_account_ids"] = ["account-one"]
            path.write_text(json.dumps(concurrent), encoding="utf-8")
        return real_atomic_write(
            path,
            value,
            expected_content=expected_content,
        )

    monkeypatch.setattr(codex_accounts, "_atomic_write_json", racing_atomic_write)

    manager.switch_account("2")

    saved = json.loads(manager.accounts_path.read_text(encoding="utf-8"))
    assert calls >= 2
    assert saved["masked_account_ids"] == ["account-one"]
    assert saved["active_account_id"] == "account-two"


def test_invalid_store_and_missing_selector_are_safe(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    switcher_home.mkdir()
    (switcher_home / "accounts.json").write_text('{"accounts":"bad"}', encoding="utf-8")
    manager = _manager(tmp_path)

    with pytest.raises(CodexAccountError, match="accounts 必须是数组"):
        manager.load_store()

    (switcher_home / "accounts.json").unlink()
    with pytest.raises(CodexAccountError, match="没有发现"):
        manager.resolve_account("1")


def test_process_classifier_ignores_codexbot_and_electron_helpers() -> None:
    assert not codex_accounts._looks_like_codex_process(
        "python.exe",
        r"C:\Python\python.exe",
        ["python", "-m", "codexbot.daemon"],
    )
    assert not codex_accounts._looks_like_codex_process(
        "Codex.exe",
        r"C:\Apps\Codex.exe",
        ["Codex.exe", "--type=renderer"],
    )
    assert codex_accounts._looks_like_codex_process(
        "codex.exe",
        r"C:\Tools\codex.exe",
        ["codex.exe", "app-server", r"D:\projects\codexbot"],
    )


def test_process_classifier_only_counts_chatgpt_from_the_codex_package() -> None:
    assert not codex_accounts._looks_like_codex_process(
        "ChatGPT.exe",
        (
            r"C:\Program Files\WindowsApps\OpenAI.ChatGPT_1.0.0.0_x64"
            r"__2p2nqsd0c76g0\app\ChatGPT.exe"
        ),
        [],
    )
    assert codex_accounts._looks_like_codex_process(
        "ChatGPT.exe",
        (
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64"
            r"__2p2nqsd0c76g0\app\ChatGPT.exe"
        ),
        [],
    )


def test_process_enumeration_failure_blocks_switching(monkeypatch) -> None:
    class Process:
        info = {"pid": 1234, "name": "python.exe", "exe": None, "cmdline": []}

    def broken_process_iter(_attrs):  # type: ignore[no-untyped-def]
        yield Process()
        raise codex_accounts.psutil.AccessDenied(pid=5678)

    monkeypatch.setattr(codex_accounts.psutil, "process_iter", broken_process_iter)

    with pytest.raises(CodexAccountError, match="无法确认 Codex 是否仍在运行"):
        codex_accounts.find_running_codex_processes()
