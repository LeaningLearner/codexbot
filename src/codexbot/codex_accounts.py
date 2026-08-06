from __future__ import annotations

"""Compatibility layer for accounts saved by Codex Switcher (codex_login).

The reference application stores its account catalogue in
``~/.codex-switcher/accounts.json`` and activates an account by writing the
official Codex ``auth.json``.  CodexBot deliberately does not copy those
credentials into SQLite, logs, or QQ messages; they are held in memory only
for the duration of a local account operation.
"""

import base64
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import ntpath
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any

import psutil

from .locks import FileLock
from .security import redact_secrets


CODEX_SWITCHER_HOME_ENV = "CODEX_SWITCHER_HOME"
CODEX_SWITCHER_DIR_NAME = ".codex-switcher"
MAX_ACCOUNT_JSON_BYTES = 8 * 1024 * 1024
_ACCOUNT_SWITCH_LOCK = threading.Lock()


class CodexAccountError(RuntimeError):
    """A local Codex account-store or account-switching failure."""


class _ConcurrentStoreChange(RuntimeError):
    """The codex_login catalogue changed during a read-modify-write."""


@dataclass(frozen=True)
class CodexTokens:
    id_token: str = field(repr=False)
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    account_id: str | None = None


@dataclass(frozen=True)
class CodexAccount:
    """Display-safe metadata plus credentials kept only in memory."""

    id: str
    name: str
    email: str | None
    plan: str | None
    auth_type: str
    auth_state: str
    tokens: CodexTokens | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)

    @property
    def is_chatgpt(self) -> bool:
        return self.auth_type == "chatgpt" and self.tokens is not None

    @property
    def is_ready(self) -> bool:
        return self.auth_state != "reauth_required"

    @property
    def display_name(self) -> str:
        return self.email or self.name or self.id


@dataclass(frozen=True)
class CodexAccountStore:
    accounts: tuple[CodexAccount, ...]
    active_account_id: str | None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    return next((value for key in keys if (value := _text(mapping.get(key)))), None)


def _normalize_auth_type(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if normalized in {"chatgpt", "chatgptlogin", "openaiauth", "oauth"}:
        return "chatgpt"
    if normalized in {"apikey", "openaiapikey"}:
        return "api_key"
    return "unknown"


def _normalize_auth_state(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "ready").casefold())
    if normalized == "ready":
        return "ready"
    # Match codex_login's two-state model and fail closed for any future or
    # malformed state instead of treating a disabled/revoked account as ready.
    return "reauth_required"


def _parse_tokens(value: object) -> CodexTokens | None:
    raw = _as_mapping(value)
    id_token = _text(raw.get("id_token") or raw.get("idToken"))
    access_token = _text(raw.get("access_token") or raw.get("accessToken"))
    refresh_token = _text(raw.get("refresh_token") or raw.get("refreshToken"))
    account_id = _text(raw.get("account_id") or raw.get("accountId"))
    if not id_token or not access_token or not refresh_token:
        return None
    return CodexTokens(id_token, access_token, refresh_token, account_id)


def _parse_account(value: object, index: int) -> CodexAccount:
    raw = _as_mapping(value)
    account_id = _text(raw.get("id"))
    if not account_id:
        raise CodexAccountError(f"accounts.json 第 {index} 个账号缺少 id")

    auth_data = _as_mapping(raw.get("auth_data") or raw.get("authData"))
    auth_type = _normalize_auth_type(
        raw.get("auth_mode") or raw.get("authMode") or auth_data.get("type")
    )
    nested_tokens = auth_data.get("tokens")
    token_source = nested_tokens if isinstance(nested_tokens, Mapping) else auth_data
    tokens = _parse_tokens(token_source)
    api_key = _first_text(auth_data, "key", "OPENAI_API_KEY", "openai_api_key")
    if api_key is None:
        api_key = _first_text(raw, "OPENAI_API_KEY", "openai_api_key")
    if auth_type == "unknown":
        if tokens is not None:
            auth_type = "chatgpt"
        elif api_key:
            auth_type = "api_key"

    email = _first_text(raw, "email", "email_address", "emailAddress")
    name = _first_text(raw, "name") or email or f"Codex 账号 {account_id[-8:]}"
    return CodexAccount(
        id=account_id,
        name=name,
        email=email,
        plan=_first_text(raw, "plan_type", "planType", "plan"),
        auth_type=auth_type,
        auth_state=_normalize_auth_state(raw.get("auth_state") or raw.get("authState")),
        tokens=tokens,
        api_key=api_key,
    )


def _read_json_snapshot(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        content = path.read_bytes()
        if len(content) > MAX_ACCOUNT_JSON_BYTES:
            raise CodexAccountError(f"账号文件过大：{path.name}")
    except CodexAccountError:
        raise
    except OSError as exc:
        raise CodexAccountError(f"无法读取 Codex 账号文件：{type(exc).__name__}") from None
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise CodexAccountError("Codex 账号文件不是有效 JSON") from None
    if not isinstance(value, dict):
        raise CodexAccountError("Codex 账号文件根节点必须是对象")
    return value, content


def _read_json(path: Path) -> dict[str, Any]:
    value, _content = _read_json_snapshot(path)
    return value


def _store_from_root(root: Mapping[str, Any]) -> CodexAccountStore:
    raw_accounts = root.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise CodexAccountError("Codex 账号文件中的 accounts 必须是数组")
    accounts = tuple(_parse_account(value, index) for index, value in enumerate(raw_accounts, 1))
    ids = [account.id for account in accounts]
    if len(ids) != len(set(ids)):
        raise CodexAccountError("Codex 账号文件包含重复的账号 ID")
    active = _text(root.get("active_account_id") or root.get("activeAccountId"))
    if active is not None and active not in set(ids):
        raise CodexAccountError("Codex 账号文件的当前账号 ID 不存在")
    return CodexAccountStore(accounts, active)


def _atomic_write_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    expected_content: bytes | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        if expected_content is not None:
            try:
                current_content = path.read_bytes()
            except OSError as exc:
                raise _ConcurrentStoreChange from exc
            if current_content != expected_content:
                raise _ConcurrentStoreChange
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise CodexAccountError(f"无法写入 Codex 账号文件：{type(exc).__name__}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
        if os.name != "nt":
            path.chmod(0o600)
    except OSError as exc:
        raise CodexAccountError(f"无法恢复 Codex 登录文件：{type(exc).__name__}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _update_account_store(
    path: Path,
    mutate: Callable[[dict[str, Any]], bool],
) -> None:
    """Apply a catalogue mutation without overwriting a completed GUI write.

    Codex Switcher uses its own in-process mutex and cannot participate in
    CodexBot's lock file.  Comparing the exact bytes immediately before the
    atomic replace closes the practical lost-update window and retries when a
    background token refresh wins the race.
    """

    lock = FileLock(path.with_suffix(path.suffix + ".lock"), timeout=2.0)
    try:
        with lock:
            for _attempt in range(3):
                root, original = _read_json_snapshot(path)
                if not mutate(root):
                    return
                try:
                    _atomic_write_json(path, root, expected_content=original)
                except _ConcurrentStoreChange:
                    continue
                return
    except TimeoutError:
        raise CodexAccountError("Codex 账号文件正被其他操作占用，请稍后重试") from None
    raise CodexAccountError("Codex 账号文件持续变化，请稍后重试")


def _jwt_claims(id_token: str) -> tuple[str | None, str | None, str | None]:
    """Read non-secret identity claims without validating the JWT signature."""

    parts = id_token.split(".")
    if len(parts) != 3:
        return None, None, None
    try:
        encoded = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, Mapping):
        return None, None, None
    auth = _as_mapping(payload.get("https://api.openai.com/auth"))
    return (
        _text(payload.get("email")),
        _text(auth.get("chatgpt_plan_type")),
        _text(auth.get("chatgpt_account_id")),
    )


def _tokens_from_auth(value: Mapping[str, Any]) -> tuple[CodexTokens | None, str | None]:
    api_key = _text(value.get("OPENAI_API_KEY") or value.get("openai_api_key"))
    return _parse_tokens(value.get("tokens")), api_key


def _same_identity(account: CodexAccount, tokens: CodexTokens) -> bool:
    email, _plan, token_claim_id = _jwt_claims(tokens.id_token)
    current_id = tokens.account_id or token_claim_id
    stored_id = None
    if account.tokens is not None:
        _stored_email, _stored_plan, stored_claim_id = _jwt_claims(
            account.tokens.id_token
        )
        stored_id = account.tokens.account_id or stored_claim_id
    if stored_id and current_id:
        return stored_id == current_id
    if account.email and email:
        return account.email.casefold() == email.casefold()
    return False


def _safe_display(value: object, *, fallback: str = "未知", limit: int = 120) -> str:
    text = redact_secrets(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())[:limit]
    return text or fallback


def _process_stem(value: object) -> str:
    name = ntpath.basename(str(value or "").replace("/", "\\")).casefold()
    stem, suffix = ntpath.splitext(name)
    return stem if suffix in {".exe", ".cmd", ".bat", ".com", ".js"} else name


def _is_codex_package_chatgpt_path(value: object) -> bool:
    normalized = str(value or "").strip().strip('"').replace("/", "\\").casefold()
    if not normalized.endswith("\\app\\chatgpt.exe"):
        return False
    package_path = normalized[: -len("\\app\\chatgpt.exe")]
    package_name = ntpath.basename(package_path)
    package_parent = ntpath.basename(ntpath.dirname(package_path))
    return package_parent == "windowsapps" and package_name.startswith("openai.codex_")


def _looks_like_codex_process(
    name: object,
    executable: object,
    command_line: object,
) -> bool:
    arguments = [str(item) for item in command_line] if isinstance(command_line, (list, tuple)) else []
    lowered = " ".join(arguments).casefold()
    if any(argument.casefold().startswith("--type=") for argument in arguments):
        return False

    stems = {_process_stem(name), _process_stem(executable)}
    stems.discard("")
    stems -= {"codex-switcher", "codex_switcher", "codex.switcher"}
    if any(stem == "codex" or stem.startswith(("codex-", "codex_")) for stem in stems):
        return True
    if "chatgpt" in stems and any(
        _is_codex_package_chatgpt_path(path)
        for path in (executable, *(arguments[:1]))
    ):
        return True
    if any(
        stem in {"codexdesktop", "codex-desktop", "codex_desktop"}
        or ("codex" in stem and "desktop" in stem)
        for stem in stems
    ):
        return True
    if "codexbot" in lowered:
        return False
    return any(_process_stem(argument) in {"codex", "codex-cli"} for argument in arguments[:4])


def find_running_codex_processes() -> tuple[int, ...]:
    """Return Codex/ChatGPT PIDs that make replacing auth.json unsafe."""

    current_pid = os.getpid()
    result: list[int] = []
    try:
        processes = psutil.process_iter(["pid", "name", "exe", "cmdline"])
        for process in processes:
            try:
                info = process.info
                pid = int(info.get("pid") or 0)
                if not pid or pid == current_pid:
                    continue
                if _looks_like_codex_process(
                    info.get("name"),
                    info.get("exe"),
                    info.get("cmdline"),
                ):
                    result.append(pid)
            except (KeyError, TypeError, ValueError, psutil.Error, OSError):
                continue
    except (OSError, psutil.Error):
        raise CodexAccountError(
            "无法确认 Codex 是否仍在运行；为保护登录凭据，本次未切换账号"
        ) from None
    return tuple(sorted(set(result)))


class CodexAccountManager:
    """Read codex_login's account store and activate official Codex auth."""

    def __init__(
        self,
        *,
        switcher_home: str | os.PathLike[str] | None = None,
        codex_home: str | os.PathLike[str] | None = None,
        process_checker: Callable[[], tuple[int, ...]] | None = None,
    ) -> None:
        switcher_override = switcher_home or os.environ.get(CODEX_SWITCHER_HOME_ENV)
        self.switcher_home = (
            Path(switcher_override).expanduser()
            if switcher_override
            else Path.home() / CODEX_SWITCHER_DIR_NAME
        )
        codex_override = codex_home or os.environ.get("CODEX_HOME")
        self.codex_home = (
            Path(codex_override).expanduser()
            if codex_override
            else Path.home() / ".codex"
        )
        self._process_checker = process_checker or find_running_codex_processes

    @property
    def accounts_path(self) -> Path:
        return self.switcher_home / "accounts.json"

    @property
    def auth_path(self) -> Path:
        return self.codex_home / "auth.json"

    def load_store(self) -> CodexAccountStore:
        if not self.accounts_path.is_file():
            return CodexAccountStore((), None)
        return _store_from_root(_read_json(self.accounts_path))

    def has_saved_accounts(self) -> bool:
        return bool(self.load_store().accounts)

    def _read_current_auth(self) -> tuple[CodexTokens | None, str | None]:
        if not self.auth_path.is_file():
            return None, None
        return _tokens_from_auth(_read_json(self.auth_path))

    def get_active_account(self) -> CodexAccount | None:
        store = self.load_store()
        account = next(
            (item for item in store.accounts if item.id == store.active_account_id),
            None,
        )
        current_tokens, current_api_key = self._read_current_auth()
        if account is not None:
            if account.is_chatgpt and current_tokens is not None and _same_identity(account, current_tokens):
                email, plan, _account_id = _jwt_claims(current_tokens.id_token)
                return replace(
                    account,
                    email=email or account.email,
                    plan=plan or account.plan,
                    tokens=current_tokens,
                )
            if account.auth_type == "api_key" and current_api_key:
                return replace(account, api_key=current_api_key)
            return account
        if current_tokens is not None:
            email, plan, account_id = _jwt_claims(current_tokens.id_token)
            return CodexAccount(
                id=account_id or current_tokens.account_id or "current",
                name=email or "当前 Codex 账号",
                email=email,
                plan=plan,
                auth_type="chatgpt",
                auth_state="ready",
                tokens=current_tokens,
            )
        if current_api_key:
            return CodexAccount(
                id="current",
                name="当前 API key",
                email=None,
                plan=None,
                auth_type="api_key",
                auth_state="ready",
                api_key=current_api_key,
            )
        return None

    def list_accounts(self) -> tuple[CodexAccount, ...]:
        store = self.load_store()
        if store.accounts:
            return store.accounts
        active = self.get_active_account()
        return (active,) if active is not None else ()

    @staticmethod
    def _resolve_from(accounts: tuple[CodexAccount, ...], selector: str) -> CodexAccount:
        if not accounts:
            raise CodexAccountError("没有发现 codex_login 保存的账号")
        value = selector.strip()
        if not value:
            raise CodexAccountError("请指定账号序号、名称、邮箱或账号 ID")
        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(accounts):
                return accounts[index - 1]
            raise CodexAccountError(f"账号序号无效，可用范围：1-{len(accounts)}")

        folded = value.casefold()
        exact = [
            account
            for account in accounts
            if folded
            in {
                account.id.casefold(),
                account.name.casefold(),
                (account.email or "").casefold(),
            }
        ]
        if len(exact) == 1:
            return exact[0]
        prefix = [account for account in accounts if account.id.casefold().startswith(folded)]
        if len(prefix) == 1:
            return prefix[0]
        if len(exact) > 1 or len(prefix) > 1:
            raise CodexAccountError("账号选择不唯一，请使用序号或完整账号 ID")
        safe_value = _safe_display(value, fallback="未知账号", limit=100)
        raise CodexAccountError(f"找不到账号：{safe_value}")

    def resolve_account(self, selector: str) -> CodexAccount:
        return self._resolve_from(self.load_store().accounts, selector)

    def _write_auth(self, account: CodexAccount) -> None:
        if account.auth_type == "api_key":
            if not account.api_key:
                raise CodexAccountError("该账号缺少 API key")
            payload: dict[str, Any] = {
                "OPENAI_API_KEY": account.api_key,
            }
        elif account.is_chatgpt:
            assert account.tokens is not None
            payload = {
                "tokens": {
                    "id_token": account.tokens.id_token,
                    "access_token": account.tokens.access_token,
                    "refresh_token": account.tokens.refresh_token,
                    **(
                        {"account_id": account.tokens.account_id}
                        if account.tokens.account_id
                        else {}
                    ),
                },
                "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        else:
            raise CodexAccountError("该账号不是可切换的 ChatGPT OAuth 或 API key 账号")
        _atomic_write_json(self.auth_path, payload)

    def reconcile_active_account(self) -> None:
        """Persist refreshed active credentials from auth.json back to the store.

        Codex can rotate OAuth tokens while it runs.  Capturing the latest
        credentials before switching away prevents a later switch back from
        restoring an obsolete refresh token.  Identity is checked before any
        ChatGPT credentials are copied so a manually changed auth.json can
        never be assigned to the wrong saved account.
        """

        if not self.accounts_path.is_file() or not self.auth_path.is_file():
            return
        try:
            current_tokens, _current_api_key = self._read_current_auth()
        except CodexAccountError:
            # A target account can still repair a malformed current auth.json;
            # reconciliation is therefore best-effort rather than a blocker.
            return

        def mutate(root: dict[str, Any]) -> bool:
            store = _store_from_root(root)
            active = next(
                (
                    account
                    for account in store.accounts
                    if account.id == store.active_account_id
                ),
                None,
            )
            raw_accounts = root.get("accounts")
            if active is None or not isinstance(raw_accounts, list):
                return False
            raw = next(
                (
                    item
                    for item in raw_accounts
                    if isinstance(item, dict) and _text(item.get("id")) == active.id
                ),
                None,
            )
            if raw is None:
                return False

            changed = False
            auth_data = raw.get("auth_data") or raw.get("authData")
            if not isinstance(auth_data, dict):
                return False
            if (
                active.is_chatgpt
                and current_tokens is not None
                and _same_identity(active, current_tokens)
            ):
                nested = auth_data.get("tokens")
                target = nested if isinstance(nested, dict) else auth_data
                updates = {
                    "id_token": current_tokens.id_token,
                    "access_token": current_tokens.access_token,
                    "refresh_token": current_tokens.refresh_token,
                }
                if current_tokens.account_id:
                    updates["account_id"] = current_tokens.account_id
                for key, value in updates.items():
                    if target.get(key) != value:
                        target[key] = value
                        changed = True
                email, plan, _account_id = _jwt_claims(current_tokens.id_token)
                if email and raw.get("email") != email:
                    raw["email"] = email
                    changed = True
                if plan and raw.get("plan_type") != plan:
                    raw["plan_type"] = plan
                    changed = True
            else:
                # API keys do not rotate and carry no account identity.
                # Copying a manually replaced key into the active record
                # could silently overwrite a different saved API account.
                return False

            if raw.get("auth_state") != "ready":
                raw["auth_state"] = "ready"
                changed = True
            return changed

        _update_account_store(self.accounts_path, mutate)

    def _mark_active(self, account_id: str) -> None:
        if not self.accounts_path.is_file():
            return
        def mutate(root: dict[str, Any]) -> bool:
            raw_accounts = root.get("accounts")
            if not isinstance(raw_accounts, list) or not any(
                isinstance(item, Mapping) and _text(item.get("id")) == account_id
                for item in raw_accounts
            ):
                raise CodexAccountError("账号在保存过程中已消失，请重试")
            root["active_account_id"] = account_id
            root.pop("activeAccountId", None)
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for item in raw_accounts:
                if isinstance(item, dict) and _text(item.get("id")) == account_id:
                    item["last_used_at"] = timestamp
            return True

        _update_account_store(self.accounts_path, mutate)

    def switch_account(self, selector: str) -> CodexAccount:
        if not _ACCOUNT_SWITCH_LOCK.acquire(timeout=2.0):
            raise CodexAccountError("另一个 Codex 账号切换正在进行，请稍后重试")
        try:
            return self._switch_account_unlocked(selector)
        finally:
            _ACCOUNT_SWITCH_LOCK.release()

    def _switch_account_unlocked(self, selector: str) -> CodexAccount:
        account = self.resolve_account(selector)
        running = tuple(self._process_checker())
        if running:
            raise CodexAccountError(
                f"检测到 {len(running)} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号"
            )

        self.reconcile_active_account()
        # Re-read after reconciliation so switching to the current account also
        # uses a just-rotated token set.
        account = self.resolve_account(selector)
        if not account.is_ready:
            raise CodexAccountError("该账号登录已过期，请先在 codex_login 中重新认证")

        # Re-check immediately before touching auth.json.  Reconciliation may
        # wait for a catalogue lock, during which a new Codex window could be
        # opened after the first safety check.
        running = tuple(self._process_checker())
        if running:
            raise CodexAccountError(
                f"检测到 {len(running)} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号"
            )

        try:
            previous_auth = self.auth_path.read_bytes() if self.auth_path.is_file() else None
        except OSError as exc:
            raise CodexAccountError(
                f"无法备份当前 Codex 登录：{type(exc).__name__}"
            ) from None
        self._write_auth(account)
        try:
            self._mark_active(account.id)
        except Exception:
            try:
                if previous_auth is None:
                    self.auth_path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(self.auth_path, previous_auth)
            except Exception as rollback_exc:
                raise CodexAccountError(
                    "账号目录同步失败，并且原 Codex 登录也无法恢复；请在 codex_login 中重新选择账号"
                ) from rollback_exc
            raise
        return account


def format_account_list(
    accounts: tuple[CodexAccount, ...],
    active_id: str | None,
) -> str:
    if not accounts:
        return "没有发现 codex_login 保存的账号。"
    lines = ["codex_login 已保存账号："]
    for index, account in enumerate(accounts, 1):
        markers = []
        if account.id == active_id:
            markers.append("当前")
        if not account.is_ready:
            markers.append("登录过期")
        suffix = f"（{'、'.join(markers)}）" if markers else ""
        email = _safe_display(account.email, fallback="无邮箱")
        lines.append(f"{index}. {_safe_display(account.name)} · {email}{suffix}")
    return "\n".join(lines)


def format_saved_account_text(account: CodexAccount) -> str:
    auth = "ChatGPT 登录" if account.auth_type == "chatgpt" else "API key"
    return "\n".join(
        [
            "Codex 当前账号（codex_login）",
            f"名称：{_safe_display(account.name)}",
            f"邮箱：{_safe_display(account.email)}",
            f"套餐：{_safe_display(account.plan)}",
            f"认证类型：{auth}",
            f"状态：{'正常' if account.is_ready else '需要重新登录'}",
        ]
    )
