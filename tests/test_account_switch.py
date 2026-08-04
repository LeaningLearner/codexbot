from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import codexbot.account_switch as account_switch


def _make_jwt(payload: dict[str, object]) -> str:
    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    header = b64(b'{"alg":"none","typ":"JWT"}')
    body = b64(json.dumps(payload).encode("utf-8"))
    return f"{header}.{body}.sig"


def _write_auth(tmp_path: Path, *, email: str = "alice@example.com", account_id: str = "acct-1") -> Path:
    auth_dir = tmp_path / ".codex"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth = auth_dir / "auth.json"
    payload = {
        "tokens": {
            "access_token": "fake-access-token",
            "refresh_token": "fake-refresh-token",
            "id_token": _make_jwt({"email": email, "sub": account_id}),
            "expires_at": 9999999999,
        },
        "account_id": account_id,
    }
    auth.write_text(json.dumps(payload), encoding="utf-8")
    return auth


@pytest.fixture
def isolated(monkeypatch, tmp_path: Path) -> None:
    """Point auth file and data dir at a temporary location."""

    def _auth_path() -> Path:
        return tmp_path / ".codex" / "auth.json"

    def _data_dir() -> Path:
        return tmp_path / "data"

    monkeypatch.setattr(account_switch, "auth_file_path", _auth_path)
    monkeypatch.setattr(account_switch, "data_dir", _data_dir)


def test_save_then_list(monkeypatch, tmp_path: Path, isolated: None) -> None:
    _write_auth(tmp_path, email="alice@example.com", account_id="acct-1")
    snapshot = account_switch.save_current_account("我的账号")
    assert snapshot.name == "我的账号"
    assert snapshot.email == "alice@example.com"
    assert snapshot.account_id == "acct-1"

    accounts = account_switch.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].name == "我的账号"
    assert accounts[0].email == "alice@example.com"


def test_switch_replaces_auth_file(monkeypatch, tmp_path: Path, isolated: None) -> None:
    _write_auth(tmp_path, email="alice@example.com", account_id="acct-1")
    account_switch.save_current_account("alice")
    # Now log in as a different account and switch back.
    _write_auth(tmp_path, email="bob@example.com", account_id="acct-2")

    email, account_id = account_switch.switch_account("alice")

    assert email == "alice@example.com"
    assert account_id == "acct-1"
    active = json.loads(account_switch.auth_file_path().read_text(encoding="utf-8"))
    assert active["account_id"] == "acct-1"


def test_switch_backs_up_current(monkeypatch, tmp_path: Path, isolated: None) -> None:
    _write_auth(tmp_path, email="alice@example.com", account_id="acct-1")
    account_switch.save_current_account("alice")
    _write_auth(tmp_path, email="bob@example.com", account_id="acct-2")

    account_switch.switch_account("alice")

    backups = list(account_switch.backups_dir().glob("*.enc"))
    assert len(backups) == 1  # bob's login was backed up


def test_delete_account(monkeypatch, tmp_path: Path, isolated: None) -> None:
    _write_auth(tmp_path)
    account_switch.save_current_account("alice")
    account_switch.delete_account("alice")
    assert account_switch.list_accounts() == []
    with pytest.raises(account_switch.SnapshotNotFoundError):
        account_switch.delete_account("alice")


def test_save_requires_login(monkeypatch, tmp_path: Path, isolated: None) -> None:
    with pytest.raises(account_switch.NotLoggedInError):
        account_switch.save_current_account("nobody")


def test_save_rejects_non_chatgpt_auth(monkeypatch, tmp_path: Path, isolated: None) -> None:
    auth = _write_auth(tmp_path)
    auth.write_text(json.dumps({"api_key": "sk-test"}), encoding="utf-8")
    with pytest.raises(account_switch.AccountSwitchError):
        account_switch.save_current_account("apikey")


def test_current_account_email(monkeypatch, tmp_path: Path, isolated: None) -> None:
    assert account_switch.current_account_email() is None
    _write_auth(tmp_path, email="carol@example.com")
    assert account_switch.current_account_email() == "carol@example.com"
