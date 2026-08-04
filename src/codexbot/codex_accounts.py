from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import base64
import json
import ntpath
import os
from pathlib import Path
import re
import tempfile
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import psutil

from .codex_usage import format_usage_text, parse_rate_limits, usage_dashboard_hint
from .locks import FileLock
from .security import redact_secrets


CODEX_SWITCHER_HOME_ENV = "CODEX_SWITCHER_HOME"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_SWITCHER_DIR_NAME = ".codex-switcher"
DEFAULT_USAGE_TIMEOUT = 20.0
MAX_JSON_BYTES = 2 * 1024 * 1024
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


class CodexAccountError(RuntimeError):
    """A local Codex account-store or account-switching failure."""


class CodexUsageError(CodexAccountError):
    """The direct ChatGPT usage endpoint could not be read."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class CodexTokens:
    id_token: str
    access_token: str
    refresh_token: str
    account_id: str | None = None


@dataclass(frozen=True)
class CodexAccount:
    """Safe account metadata plus in-memory credentials needed for requests."""

    id: str
    name: str
    email: str | None
    plan: str | None
    auth_type: str
    auth_state: str
    tokens: CodexTokens | None = None
    api_key: str | None = None

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
    return "reauth_required" if normalized in {"reauthrequired", "expired"} else "ready"


def _parse_tokens(value: object) -> CodexTokens | None:
    raw = _as_mapping(value)
    id_token = _text(raw.get("id_token") or raw.get("idToken"))
    access_token = _text(raw.get("access_token") or raw.get("accessToken"))
    refresh_token = _text(raw.get("refresh_token") or raw.get("refreshToken"))
    account_id = _text(raw.get("account_id") or raw.get("accountId"))
    if not id_token or not access_token or not refresh_token:
        return None
    return CodexTokens(
        id_token=id_token,
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
    )


def _parse_account(value: object, index: int) -> CodexAccount:
    raw = _as_mapping(value)
    account_id = _text(raw.get("id"))
    if not account_id:
        raise CodexAccountError(f"accounts.json 第 {index} 个账号缺少 id")

    auth_data = _as_mapping(raw.get("auth_data") or raw.get("authData"))
    auth_type = _normalize_auth_type(
        raw.get("auth_mode") or raw.get("authMode") or auth_data.get("type")
    )
    token_source = auth_data.get("tokens") if isinstance(auth_data.get("tokens"), Mapping) else auth_data
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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexAccountError(f"无法读取 Codex 账号文件：{type(exc).__name__}") from None
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        raise CodexAccountError("Codex 账号文件不是有效 JSON") from None
    if not isinstance(value, dict):
        raise CodexAccountError("Codex 账号文件根节点必须是对象")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
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
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
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
            temporary = Path(handle.name)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise CodexAccountError(f"无法恢复 Codex 登录文件：{type(exc).__name__}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


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
    tokens = _parse_tokens(value.get("tokens"))
    return tokens, api_key


def _same_identity(account: CodexAccount, tokens: CodexTokens) -> bool:
    email, _plan, account_id = _jwt_claims(tokens.id_token)
    stored_id = account.tokens.account_id if account.tokens else None
    if stored_id and account_id:
        return stored_id == account_id
    if account.email and email:
        return account.email.casefold() == email.casefold()
    return False


def _safe_display(value: object, *, fallback: str = "未知", limit: int = 100) -> str:
    result = redact_secrets(str(value or "")).replace("\r", " ").replace("\n", " ")
    result = " ".join(result.split())[:limit]
    return result or fallback


def _process_stem(value: object) -> str:
    name = ntpath.basename(str(value or "").replace("/", "\\")).casefold()
    stem, suffix = ntpath.splitext(name)
    if suffix in {".exe", ".cmd", ".bat", ".com", ".js"}:
        return stem
    return name


def _looks_like_codex_process(name: object, executable: object, command_line: object) -> bool:
    stems = {_process_stem(name), _process_stem(executable)}
    stems.discard("")
    stems -= {"codex-switcher", "codex_switcher", "codex.switcher"}
    if any(stem == "codex" or stem.startswith(("codex-", "codex_")) for stem in stems):
        return True
    if any(
        stem in {"chatgpt", "codexdesktop", "codex-desktop", "codex_desktop"}
        or ("codex" in stem and "desktop" in stem)
        for stem in stems
    ):
        return True
    arguments = [str(item) for item in command_line] if isinstance(command_line, (list, tuple)) else []
    lowered = " ".join(arguments).casefold()
    if "codexbot" in lowered or "codex-switcher" in lowered:
        return False
    return any(_process_stem(argument) in {"codex", "codex-cli"} for argument in arguments[:4])


def find_running_codex_processes() -> tuple[int, ...]:
    """Return active Codex/ChatGPT host PIDs that make auth switching unsafe."""

    current_pid = os.getpid()
    result: list[int] = []
    try:
        processes = psutil.process_iter(["pid", "name", "exe", "cmdline"])
    except (OSError, psutil.Error):
        return ()
    for process in processes:
        try:
            info = process.info
            pid = int(info.get("pid") or 0)
            if not pid or pid == current_pid:
                continue
            if _looks_like_codex_process(info.get("name"), info.get("exe"), info.get("cmdline")):
                result.append(pid)
        except (KeyError, TypeError, ValueError, psutil.Error, OSError):
            continue
    return tuple(sorted(set(result)))


class CodexAccountManager:
    """Read codex_login's account store and switch official Codex auth."""

    def __init__(
        self,
        *,
        switcher_home: str | os.PathLike[str] | None = None,
        codex_home: str | os.PathLike[str] | None = None,
        urlopen_factory: Callable[..., Any] | None = None,
        process_checker: Callable[[], tuple[int, ...]] | None = None,
    ) -> None:
        switcher_override = switcher_home or os.environ.get(CODEX_SWITCHER_HOME_ENV)
        self.switcher_home = Path(switcher_override).expanduser() if switcher_override else Path.home() / CODEX_SWITCHER_DIR_NAME
        codex_override = codex_home or os.environ.get("CODEX_HOME")
        self.codex_home = Path(codex_override).expanduser() if codex_override else Path.home() / ".codex"
        self._urlopen = urlopen_factory or urlopen
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
        root = _read_json(self.accounts_path)
        raw_accounts = root.get("accounts")
        if raw_accounts is None:
            raw_accounts = []
        if not isinstance(raw_accounts, list):
            raise CodexAccountError("Codex 账号文件中的 accounts 必须是数组")
        accounts = tuple(_parse_account(value, index) for index, value in enumerate(raw_accounts, 1))
        active = _text(root.get("active_account_id") or root.get("activeAccountId"))
        return CodexAccountStore(accounts, active)

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
                id=account_id or "current",
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

    def resolve_account(self, selector: str) -> CodexAccount:
        store = self.load_store()
        accounts = store.accounts
        if not accounts:
            raise CodexAccountError("没有发现 codex_login 保存的账号")
        value = selector.strip()
        if not value:
            raise CodexAccountError("请指定账号序号、名称或账号 ID")

        if value.isdigit():
            index = int(value)
            if 1 <= index <= len(accounts):
                return accounts[index - 1]
            raise CodexAccountError(f"账号序号无效，可用范围：1-{len(accounts)}")

        exact = [
            account
            for account in accounts
            if value.casefold() in {account.id.casefold(), account.name.casefold(), (account.email or "").casefold()}
        ]
        if len(exact) == 1:
            return exact[0]
        prefix = [account for account in accounts if account.id.casefold().startswith(value.casefold())]
        if len(prefix) == 1:
            return prefix[0]
        if len(exact) > 1 or len(prefix) > 1:
            raise CodexAccountError("账号名称不唯一，请使用序号或完整账号 ID")
        raise CodexAccountError(f"找不到账号：{_safe_display(value)}")

    def _write_auth(self, account: CodexAccount) -> None:
        if account.auth_type == "api_key":
            if not account.api_key:
                raise CodexAccountError("该账号缺少 API key")
            payload: dict[str, Any] = {"OPENAI_API_KEY": account.api_key}
        elif account.is_chatgpt:
            assert account.tokens is not None
            payload = {
                "tokens": {
                    "id_token": account.tokens.id_token,
                    "access_token": account.tokens.access_token,
                    "refresh_token": account.tokens.refresh_token,
                    **({"account_id": account.tokens.account_id} if account.tokens.account_id else {}),
                },
                "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        else:
            raise CodexAccountError("该账号不是可切换的 ChatGPT OAuth 或 API key 账号")
        _atomic_write_json(self.auth_path, payload)

    def _mark_active(self, account_id: str) -> None:
        if not self.accounts_path.is_file():
            return
        lock = FileLock(self.accounts_path.with_suffix(self.accounts_path.suffix + ".lock"), timeout=2.0)
        try:
            with lock:
                root = _read_json(self.accounts_path)
                raw_accounts = root.get("accounts")
                if not isinstance(raw_accounts, list) or not any(
                    isinstance(item, Mapping) and _text(item.get("id")) == account_id for item in raw_accounts
                ):
                    raise CodexAccountError("账号在保存过程中已消失，请重试")
                root["active_account_id"] = account_id
                for item in raw_accounts:
                    if isinstance(item, dict) and _text(item.get("id")) == account_id:
                        item["last_used_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                _atomic_write_json(self.accounts_path, root)
        except TimeoutError:
            raise CodexAccountError("Codex 账号文件正被其他操作占用，请稍后重试") from None

    def switch_account(self, selector: str) -> CodexAccount:
        account = self.resolve_account(selector)
        if not account.is_ready:
            raise CodexAccountError("该账号登录已过期，请先在 codex_login 中重新认证")
        running = tuple(self._process_checker())
        if running:
            raise CodexAccountError(
                f"检测到 {len(running)} 个 Codex/ChatGPT 进程正在运行，请关闭后再切换账号"
            )
        previous_auth = self.auth_path.read_bytes() if self.auth_path.is_file() else None
        self._write_auth(account)
        try:
            self._mark_active(account.id)
        except Exception:
            # Do not leave auth.json pointing at an account while the switcher
            # store still claims another account is active.
            try:
                if previous_auth is None:
                    self.auth_path.unlink(missing_ok=True)
                else:
                    _atomic_write_bytes(self.auth_path, previous_auth)
            except Exception:
                pass
            raise
        return account

    def read_usage(self, account: CodexAccount | None = None, *, timeout: float = DEFAULT_USAGE_TIMEOUT) -> Mapping[str, Any]:
        account = account or self.get_active_account()
        if account is None:
            raise CodexUsageError("当前没有可用的 Codex 登录")
        if not account.is_ready:
            raise CodexUsageError("当前账号登录已过期", status=401)
        if not account.is_chatgpt:
            raise CodexUsageError("当前账号不是可读取用量的 ChatGPT OAuth 账号")
        assert account.tokens is not None
        headers = {
            "Authorization": f"Bearer {account.tokens.access_token}",
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if account.tokens.account_id:
            headers["chatgpt-account-id"] = account.tokens.account_id
        request = Request(CODEX_USAGE_URL, headers=headers, method="GET")
        try:
            response = self._urlopen(request, timeout=max(float(timeout), 0.1))
        except HTTPError as exc:
            raise CodexUsageError(f"用量接口返回 HTTP {exc.code}", status=exc.code) from None
        except (URLError, OSError, ValueError) as exc:
            raise CodexUsageError(f"用量接口连接失败：{type(exc).__name__}") from None
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(MAX_JSON_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise CodexUsageError(f"用量接口读取失败：{type(exc).__name__}") from None
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                try:
                    close()
                except OSError:
                    pass
        if int(status or 0) < 200 or int(status or 0) >= 300:
            raise CodexUsageError(f"用量接口返回 HTTP {int(status or 0)}", status=int(status or 0))
        if len(body) > MAX_JSON_BYTES:
            raise CodexUsageError("用量接口返回内容过大")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CodexUsageError("用量接口返回了无法解析的数据") from None
        if not isinstance(payload, Mapping):
            raise CodexUsageError("用量接口返回格式无效")
        return payload


def format_saved_usage_text(account: CodexAccount, payload: Mapping[str, Any]) -> str:
    """Format the same backend response used by codex_login's usage screen."""

    snapshot = parse_rate_limits(payload)
    lines = [f"Codex 用量：{_safe_display(account.display_name)}"]
    plan = _text(payload.get("plan_type") or payload.get("planType"))
    if plan:
        lines.append(f"套餐：{_safe_display(plan)}")
    if snapshot.buckets:
        formatted = format_usage_text(snapshot).splitlines()
        lines.extend(line for line in formatted[1:] if not line.startswith("用量面板："))
    else:
        lines.append("当前账号没有返回可用的限额 bucket。")

    credits = _as_mapping(payload.get("credits"))
    if credits:
        unlimited = credits.get("unlimited")
        balance = _text(credits.get("balance"))
        if unlimited is True:
            lines.append("积分：无限")
        elif balance:
            lines.append(f"积分余额：{_safe_display(balance)}")
        elif credits.get("has_credits") is False:
            lines.append("积分：无可用积分")
    lines.append(usage_dashboard_hint())
    return "\n".join(lines)


def format_account_list(accounts: tuple[CodexAccount, ...], active_id: str | None) -> str:
    if not accounts:
        return (
            "没有发现 codex_login 保存的账号。\n"
            "请先在 Codex Switcher 中登录/导入账号，再用 /codex_accounts 查看。"
        )
    lines = ["Codex 已保存账号"]
    for index, account in enumerate(accounts, 1):
        marker = "当前" if account.id == active_id else ""
        state = "登录过期" if not account.is_ready else ""
        tags = " · ".join(item for item in (marker, state) if item)
        suffix = f"（{tags}）" if tags else ""
        auth = "ChatGPT 登录" if account.auth_type == "chatgpt" else "API key" if account.auth_type == "api_key" else "未知认证"
        lines.append(
            f"{index}. {_safe_display(account.name)} · {_safe_display(account.email, fallback='无邮箱')} · {auth}{suffix}"
        )
    lines.append("切换：/codex_switch 序号（例如 /codex_switch 2）")
    return "\n".join(lines)


def format_saved_account_text(account: CodexAccount) -> str:
    auth = "ChatGPT 登录" if account.auth_type == "chatgpt" else "API key" if account.auth_type == "api_key" else "未知认证"
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
