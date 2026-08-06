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
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
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
    override = os.environ.get("CODEX_HOME")
    codex_home = Path(override).expanduser() if override else Path.home() / ".codex"
    return codex_home / "auth.json"


def accounts_dir() -> Path:
    return data_dir() / ACCOUNTS_SUBDIR


def backups_dir() -> Path:
    return accounts_dir() / BACKUPS_SUBDIR


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("_", name.strip())
    return slug.strip("._-") or "account"


def _snapshot_path(name: str) -> Path:
    display_name = name.strip()
    digest = hashlib.sha256(display_name.casefold().encode("utf-8")).hexdigest()[:16]
    return accounts_dir() / f"{_slugify(display_name)}--{digest}.enc"


def _legacy_snapshot_path(name: str) -> Path:
    """Path used before names gained a collision-resistant suffix."""

    return accounts_dir() / f"{_slugify(name)}.enc"


def _read_auth_json() -> dict[str, Any]:
    path = auth_file_path()
    if not path.is_file():
        raise NotLoggedInError("~/.codex/auth.json 不存在，请先在 Codex 中登录。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccountSwitchError(f"auth.json 无法解析：{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise AccountSwitchError("auth.json 的根节点必须是对象。")
    tokens = data.get("tokens")
    api_key = data.get("OPENAI_API_KEY") or data.get("openai_api_key")
    if not isinstance(tokens, dict) and not isinstance(api_key, str):
        raise AccountSwitchError("auth.json 既不包含 ChatGPT tokens，也不包含 API key，无法保存。")
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

    raw_tokens = data.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    email = data.get("email")
    account_id = data.get("account_id") or tokens.get("account_id")
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
            handle.flush()
            os.fsync(handle.fileno())
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


def _load_snapshot_path(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_dpapi_unprotect(path.read_bytes()).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidSnapshotError(f"账号快照 {path.stem!r} 无法解密或解析。") from exc
    auth_json = payload.get("auth_json") if isinstance(payload, dict) else None
    if not isinstance(auth_json, dict):
        raise InvalidSnapshotError(f"账号快照 {path.stem!r} 格式无效。")
    return payload


def _load_snapshot(name: str) -> dict[str, Any]:
    display_name = name.strip()
    for path in (_snapshot_path(display_name), _legacy_snapshot_path(display_name)):
        if not path.is_file():
            continue
        payload = _load_snapshot_path(path)
        stored_name = str(payload.get("name") or path.stem)
        if stored_name.casefold() == display_name.casefold():
            return payload

    # Legacy slug-only files can collide, and renamed snapshot files may no
    # longer be derivable from the display name. Resolve by metadata as a safe
    # compatibility fallback.
    if accounts_dir().is_dir():
        for path in accounts_dir().glob("*.enc"):
            try:
                payload = _load_snapshot_path(path)
            except AccountSwitchError:
                continue
            if str(payload.get("name") or "").casefold() == display_name.casefold():
                return payload
    raise SnapshotNotFoundError(f"未找到已保存的账号 {name!r}，请先用 /account save 保存。")


def list_accounts() -> list[AccountSnapshot]:
    accounts_dir().mkdir(parents=True, exist_ok=True)
    by_name: dict[str, AccountSnapshot] = {}
    for path in sorted(accounts_dir().glob("*.enc")):
        try:
            payload = _load_snapshot_path(path)
        except AccountSwitchError:
            continue
        try:
            saved_at = float(payload.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            continue
        snapshot = AccountSnapshot(
            name=str(payload.get("name") or path.stem),
            email=payload.get("email"),
            account_id=payload.get("account_id"),
            saved_at=saved_at,
        )
        key = snapshot.name.casefold()
        previous = by_name.get(key)
        if previous is None or snapshot.saved_at >= previous.saved_at:
            by_name[key] = snapshot
    return sorted(by_name.values(), key=lambda item: (item.name.casefold(), item.saved_at))


def _running_codex_processes() -> tuple[int, ...]:
    # Import lazily so the legacy DPAPI snapshot feature stays independent of
    # the optional codex_login-compatible account store at import time.
    from .codex_accounts import find_running_codex_processes

    return find_running_codex_processes()


def switch_account(
    name: str,
    *,
    process_checker: Callable[[], tuple[int, ...]] | None = None,
) -> tuple[str | None, str | None]:
    """Swap ~/.codex/auth.json to the saved snapshot, backing up the current one."""

    running = tuple((process_checker or _running_codex_processes)())
    if running:
        raise AccountSwitchError(
            f"检测到 {len(running)} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号。"
        )
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
            current_email, current_account_id = _account_identity(current)
            backup = {
                "email": current_email,
                "account_id": current_account_id,
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
    display_name = name.strip()
    matches: set[Path] = set()
    for candidate in (_snapshot_path(display_name), _legacy_snapshot_path(display_name)):
        if not candidate.is_file():
            continue
        try:
            payload = _load_snapshot_path(candidate)
        except AccountSwitchError:
            continue
        if str(payload.get("name") or candidate.stem).casefold() == display_name.casefold():
            matches.add(candidate)
    if accounts_dir().is_dir():
        for candidate in accounts_dir().glob("*.enc"):
            if candidate in matches:
                continue
            try:
                payload = _load_snapshot_path(candidate)
            except AccountSwitchError:
                continue
            if str(payload.get("name") or "").casefold() == display_name.casefold():
                matches.add(candidate)
    if not matches:
        raise SnapshotNotFoundError(f"未找到已保存的账号 {name!r}。")
    for path in matches:
        try:
            path.unlink()
        except OSError as exc:
            raise AccountSwitchError(
                f"删除账号 {name!r} 失败：{type(exc).__name__}"
            ) from None


def current_account_email() -> str | None:
    """Email of the active login, or None when not a ChatGPT login."""
    try:
        data = _read_auth_json()
    except AccountSwitchError:
        return None
    email, _ = _account_identity(data)
    return email
