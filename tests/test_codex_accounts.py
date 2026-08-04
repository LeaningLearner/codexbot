from __future__ import annotations

import base64
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from codexbot.codex_accounts import CodexAccountError, CodexAccountManager, format_saved_usage_text
from codexbot.commands import CommandService
from codexbot.store import Store


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
        ).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def _account(account_id: str, email: str, access: str) -> dict[str, Any]:
    return {
        "id": account_id,
        "name": email,
        "email": email,
        "plan_type": "plus",
        "auth_mode": "chat_g_p_t",
        "auth_data": {
            "type": "chat_g_p_t",
            "id_token": _id_token(email, account_id),
            "access_token": access,
            "refresh_token": f"refresh-{access}",
            "account_id": account_id,
        },
        "auth_state": "ready",
    }


def _write_store(switcher_home: Path) -> tuple[str, str]:
    first_id = "account-one"
    second_id = "account-two"
    switcher_home.mkdir(parents=True)
    (switcher_home / "accounts.json").write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    _account(first_id, "one@example.com", "access-one"),
                    _account(second_id, "two@example.com", "access-two"),
                ],
                "active_account_id": first_id,
            }
        ),
        encoding="utf-8",
    )
    return first_id, second_id


class FakeResponse:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()
        self.closed = False

    def read(self, _limit: int) -> bytes:
        return self.payload

    def close(self) -> None:
        self.closed = True


class CommandSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def passive(self, _openid: str, text: str, _message_id: str, _sequence: int) -> object:
        self.messages.append(text)
        return object()

    async def active(self, _openid: str, _text: str) -> object:
        return object()


def test_reads_codex_login_store_and_direct_usage_endpoint(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    first_id, _second_id = _write_store(switcher_home)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    first = json.loads((switcher_home / "accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {key: value for key, value in first["auth_data"].items() if key != "type"}}),
        encoding="utf-8",
    )
    requests: list[tuple[str, dict[str, str]]] = []

    def open_url(request: Any, *, timeout: float) -> FakeResponse:
        requests.append((request.full_url, dict(request.header_items())))
        return FakeResponse(
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 25,
                        "limit_window_seconds": 18_000,
                        "reset_at": 1_800_000_000,
                    },
                    "secondary_window": {
                        "used_percent": 50,
                        "limit_window_seconds": 604_800,
                        "reset_at": 1_800_100_000,
                    },
                },
                "credits": {"has_credits": True, "unlimited": False, "balance": "$10.50"},
            }
        )

    manager = CodexAccountManager(
        switcher_home=switcher_home,
        codex_home=codex_home,
        urlopen_factory=open_url,
        process_checker=lambda: (),
    )
    accounts = manager.list_accounts()
    assert [account.id for account in accounts] == [first_id, "account-two"]
    active = manager.get_active_account()
    assert active is not None
    assert active.email == "one@example.com"

    payload = manager.read_usage(active)
    text = format_saved_usage_text(active, payload)

    assert requests[0][0] == "https://chatgpt.com/backend-api/wham/usage"
    assert requests[0][1]["Authorization"] == "Bearer access-one"
    assert requests[0][1]["Chatgpt-account-id"] == first_id
    assert "primary：剩余 75%" in text
    assert "secondary：剩余 50%" in text
    assert "积分余额：$10.50" in text


def test_switch_writes_auth_json_and_updates_active_account(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _first_id, second_id = _write_store(switcher_home)
    codex_home = tmp_path / ".codex"
    manager = CodexAccountManager(
        switcher_home=switcher_home,
        codex_home=codex_home,
        process_checker=lambda: (),
    )

    switched = manager.switch_account("2")
    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    store = json.loads((switcher_home / "accounts.json").read_text(encoding="utf-8"))

    assert switched.id == second_id
    assert auth["tokens"]["access_token"] == "access-two"
    assert auth["tokens"]["account_id"] == second_id
    assert store["active_account_id"] == second_id
    assert store["accounts"][1]["last_used_at"]


def test_switch_is_blocked_while_codex_is_running(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = CodexAccountManager(
        switcher_home=switcher_home,
        codex_home=tmp_path / ".codex",
        process_checker=lambda: (1234,),
    )

    with pytest.raises(CodexAccountError, match="正在运行"):
        manager.switch_account("2")


def test_switch_rolls_auth_file_back_if_account_store_update_fails(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    original = b'{"tokens":{"access_token":"old"}}\n'
    auth_path = codex_home / "auth.json"
    auth_path.write_bytes(original)
    manager = CodexAccountManager(
        switcher_home=switcher_home,
        codex_home=codex_home,
        process_checker=lambda: (),
    )

    def fail_mark(_account_id: str) -> None:
        raise CodexAccountError("simulated store failure")

    manager._mark_active = fail_mark  # type: ignore[method-assign]
    with pytest.raises(CodexAccountError, match="simulated"):
        manager.switch_account("2")

    assert auth_path.read_bytes() == original


def test_qq_account_commands_list_and_switch_saved_accounts(tmp_path: Path) -> None:
    switcher_home = tmp_path / ".codex-switcher"
    _write_store(switcher_home)
    manager = CodexAccountManager(
        switcher_home=switcher_home,
        codex_home=tmp_path / ".codex",
        process_checker=lambda: (),
    )
    store = Store(tmp_path / "state.sqlite3")
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)
    service = CommandService(store, account_manager=manager)
    sent = CommandSender()

    async def run() -> None:
        assert await service.handle(
            openid="owner",
            message_id="accounts-command",
            content="/codex_accounts",
            passive_send=sent.passive,
            active_send=sent.active,
        ) == "replied"
        assert "1. one@example.com" in sent.messages[-1]

        assert await service.handle(
            openid="owner",
            message_id="switch-command",
            content="/codex_switch 2",
            passive_send=sent.passive,
            active_send=sent.active,
        ) == "replied"

    asyncio.run(run())
    assert "账号已切换" in sent.messages[-1]
