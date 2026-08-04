from __future__ import annotations

"""Multi-account snapshots for Codex login state.

Codex stores the active ChatGPT login in ``~/.codex/auth.json``.  This module
keeps DPAPI-encrypted snapshots of that file so the user can save several
accounts and switch between them from QQ (``/account``), in the same
spirit as third-party account switchers.

Security notes:
- Snapshots are encrypted with Windows DPAPI (current user scope).
- Tokens are never logged or sent to QQ; only the account email is shown.
- Switching writes through a temporary file and an atomic rename so Codex
  never observes a half-written auth.json.
"""

import base64
import ctypes
from ctypes import wintypes
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import data_dir


ACCOUNTS_SUBDIR = "accounts"
BACKUPS_SUBDIR = "backups"
_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class AccountSwitchError(RuntimeError):
    """Base class for account snapshot failures."""


class NotLoggedInError(AccountSwitchError):
    pass


class AccountNameError(AccountSwitchError):
    pass


class SnapshotNotFoundError(AccountSwitchError):
    pass


class InvalidSnapshotError(AccountSwitchError):
    pass


@dataclass(frozen=True)
class AccountSnapshot:
    name: str
    email: str | None
    account_id: str | None
    saved_at: float


def auth_file_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def accounts_dir() -> Path:
    return data_dir() / ACCOUNTS_SUBDIR


def backups_dir() -> Path:
    return accounts_dir() / BACKUPS_SUBDIR


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip())
    return slug.strip("._-") or "account"


def _snapshot_path(name: str) -> Path:
    return accounts_dir() / f"{_slugify(name)}.enc"


def _read_auth_json() -> dict[str, Any]:
    path = auth_file_path()
    if not path.is_file():
        raise NotLoggedInError("~/.codex/auth.json 不存在，请先在 Codex 中登录。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountSwitchError(f"auth.json 无法解析：{type(exc).__name__}") from exc
    if not isinstance(data, dict) or "tokens" not in data:
        raise AccountSwitchError("auth.json 不是 ChatGPT 登录格式（缺少 tokens），无法保存。")
    return data


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        _, payload, _ = token.split(".", 2)
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        value = json.loads(decoded.decode("utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _account_identity(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (email, account_id) from an auth.json payload without logging tokens."""

    tokens = data.get("tokens") or {}
    email = data.get("email")
    account_id = data.get("account_id")
    if not email:
        for token_name in ("id_token", "access_token"):
            token = tokens.get(token_name)
            if not isinstance(token, str):
                continue
            payload = _jwt_payload(token)
            email = payload.get("email") or payload.get("https://api.openai.com/auth.email")
            if email:
                break
    return (str(email) if email else None, str(account_id) if account_id else None)


def _dpapi_protect(plain: bytes) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buffer = ctypes.create_string_buffer(plain)
    blob_in = Blob(len(plain), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise AccountSwitchError("DPAPI 加密失败（CryptProtectData）。")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(encrypted: bytes) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    buffer = ctypes.create_string_buffer(encrypted)
    blob_in = Blob(len(encrypted), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise AccountSwitchError("DPAPI 解密失败（CryptUnprotectData）。")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _write_atomic(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".auth-switch-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def save_current_account(name: str) -> AccountSnapshot:
    """Save the active ~/.codex/auth.json as an encrypted snapshot."""

    display_name = name.strip()
    if not display_name:
        raise AccountNameError("账号名称不能为空。")
    data = _read_auth_json()
    email, account_id = _account_identity(data)
    snapshot = {
        "name": display_name,
        "email": email,
        "account_id": account_id,
        "saved_at": time.time(),
        "auth_json": data,
    }
    encrypted = _dpapi_protect(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
    path = _snapshot_path(display_name)
    _write_atomic(path, encrypted)
    return AccountSnapshot(display_name, email, account_id, snapshot["saved_at"])


def _load_snapshot(name: str) -> dict[str, Any]:
    path = _snapshot_path(name)
    if not path.is_file():
        raise SnapshotNotFoundError(f"未找到已保存的账号 {name!r}，请先用 /account save 保存。")
    try:
        payload = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvalidSnapshotError(f"账号快照 {name!r} 无法解密或解析。") from exc
    if not isinstance(payload, dict) or "auth_json" not in payload:
        raise InvalidSnapshotError(f"账号快照 {name!r} 格式无效。")
    return payload


def list_accounts() -> list[AccountSnapshot]:
    accounts_dir().mkdir(parents=True, exist_ok=True)
    result: list[AccountSnapshot] = []
    for path in sorted(accounts_dir().glob("*.enc")):
        try:
            payload = _load_snapshot(path.stem)
        except AccountSwitchError:
            continue
        result.append(
            AccountSnapshot(
                name=str(payload.get("name") or path.stem),
                email=payload.get("email"),
                account_id=payload.get("account_id"),
                saved_at=float(payload.get("saved_at") or 0.0),
            )
        )
    return result


def switch_account(name: str) -> tuple[str | None, str | None]:
    """Swap ~/.codex/auth.json to the saved snapshot, backing up the current one."""

    payload = _load_snapshot(name)
    auth_json = payload["auth_json"]
    target = auth_file_path()
    if target.is_file():
        try:
            current = _read_auth_json()
        except AccountSwitchError:
            current = None
        if current is not None:
            backups_dir().mkdir(parents=True, exist_ok=True)
            backup = {
                "email": payload.get("email"),
                "account_id": payload.get("account_id"),
                "saved_at": time.time(),
                "auth_json": current,
            }
            encrypted = _dpapi_protect(json.dumps(backup, ensure_ascii=False).encode("utf-8"))
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            _write_atomic(backups_dir() / f"before-switch-{stamp}.enc", encrypted)
    _write_atomic(target, json.dumps(auth_json, ensure_ascii=False).encode("utf-8"))
    email, account_id = _account_identity(auth_json)
    return email, account_id


def delete_account(name: str) -> None:
    path = _snapshot_path(name)
    if not path.is_file():
        raise SnapshotNotFoundError(f"未找到已保存的账号 {name!r}。")
    try:
        path.unlink()
    except OSError as exc:
        raise AccountSwitchError(f"删除账号 {name!r} 失败：{exc}") from exc


def current_account_email() -> str | None:
    """Email of the active login, or None when not a ChatGPT login."""
    try:
        data = _read_auth_json()
    except AccountSwitchError:
        return None
    email, _ = _account_identity(data)
    return email
