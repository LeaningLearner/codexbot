from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import re
from typing import Any, Awaitable, Callable

from .codex_accounts import CodexAccountError, CodexAccountManager
from .codex_login import (
    AccountInfo,
    AppServerError,
    AppServerRPCError,
    AppServerTimeout,
    CodexAppServerClient,
    parse_account_result,
)
from .codex_usage import format_usage_text, parse_rate_limits, usage_dashboard_hint
from .formatting import split_text
from .security import redact_secrets
from .store import Store
from . import account_switch


PassiveSender = Callable[[str, str, str, int], Awaitable[object]]
ActiveSender = Callable[[str, str], Awaitable[object]]


HELP_TEXT = (
    "CodexBot QQ 命令\n"
    "/bind 配对码 - 首次绑定或使用新配对码换绑\n"
    "/status - 查看 Codex 当前状态\n"
    "/last [项目] [页码] - 查看最近回复；只写页码时保持兼容\n"
    "/usage - 查看 Codex 各限额剩余百分比和重置时间\n"
    "/account - 查看当前 Codex 账号\n"
    "/account save 名称 - 保存当前 Codex 账号\n"
    "/account list - 列出 codex_login 账号和加密快照\n"
    "/account use 序号/名称/邮箱/ID - 安全切换账号\n"
    "/account delete 名称 - 删除已保存的账号\n"
    "/mute - 暂停主动通知\n"
    "/unmute - 恢复主动通知\n"
    "/help - 显示此帮助"
)


def _safe_field(value: object, *, fallback: str = "未知", limit: int = 160) -> str:
    text = redact_secrets(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())[:limit]
    return text or fallback


def _auth_type_text(value: str) -> str:
    normalized = value.casefold().replace("-", "_")
    if normalized in {"apikey", "api_key", "api key"}:
        return "API key"
    if normalized in {"chatgpt", "chatgpt_login", "openai", "openai_auth"}:
        return "ChatGPT 登录"
    if normalized == "not_logged_in":
        return "未登录"
    return _safe_field(value)


def _account_text(account: AccountInfo) -> str:
    if not account.is_authenticated:
        return (
            "Codex 当前未登录（或当前认证方式不支持 OpenAI 账号读取）。\n"
            "认证类型：未登录\n"
            "请在 Codex 中登录后重试 /account。"
        )
    return "\n".join(
        [
            "Codex 当前账号",
            f"邮箱：{_safe_field(account.email)}",
            f"套餐：{_safe_field(account.plan)}",
            f"认证类型：{_auth_type_text(account.auth_type)}",
        ]
    )


def _is_api_key_account(account: AccountInfo) -> bool:
    return account.auth_type.casefold().replace("-", "_") in {"apikey", "api_key", "api key"}


def _auth_required_error(exc: BaseException) -> bool:
    if isinstance(exc, AppServerRPCError) and exc.code == -32600:
        return True
    return "authentication required" in str(exc).casefold()


def _codex_failure_text(action: str, exc: BaseException, *, dashboard: bool = False) -> str:
    if isinstance(exc, AppServerTimeout):
        text = f"Codex {action}超时，请稍后重试。"
    elif _auth_required_error(exc):
        text = f"Codex {action}需要 ChatGPT 账号认证；当前可能未登录或使用 API key。"
    elif isinstance(exc, AppServerRPCError) and exc.code == -32601:
        text = f"当前 Codex 版本不支持 {action} 接口，请升级 Codex CLI（建议 0.146.0 或更新版本）。"
    elif isinstance(exc, AppServerError):
        text = f"Codex {action}暂不可用，可能是旧版 app-server 或进程未运行。"
    else:
        # External exception text is deliberately not sent to QQ.
        text = f"Codex {action}暂时失败，请稍后重试。"
    return f"{text}\n{usage_dashboard_hint() if dashboard else ''}".rstrip()


def _last_arguments(argument: str) -> tuple[str | None, int] | None:
    argument = argument.strip()
    if not argument:
        return None, 1
    tokens = argument.split()
    if tokens and tokens[0].casefold() in {"--project", "-p"}:
        tokens = tokens[1:]
        if not tokens:
            return None
    if tokens and tokens[0].casefold().startswith("--project="):
        tokens[0] = tokens[0].split("=", 1)[1]
    page = 1
    if tokens and tokens[-1].isdigit():
        page = int(tokens.pop())
    project = " ".join(tokens).strip() or None
    if page < 1 or project == "":
        return None
    return project, page


def _last_text(store: Store, page: int, project: str | None = None) -> str:
    reply = store.get_last_reply(project=project)
    if not reply:
        if project:
            available = store.get_last_reply_projects()
            suffix = f"可用项目：{'、'.join(available)}" if available else "当前没有可用项目"
            return f"找不到项目“{_safe_field(project)}”的 Codex 回复。{suffix}。"
        return "还没有可读取的 Codex 最终回复。"
    chunks = split_text(str(reply["content"]), limit=1000)
    if page < 1 or page > len(chunks):
        return f"页码无效，可用范围：1-{len(chunks)}。"
    title = "最近一次" if project is None else f"项目 {reply['project']} 最近一次"
    return (
        f"{title} Codex 回复 [{page}/{len(chunks)}]\n"
        f"项目：{reply['project']}\n"
        f"模型：{reply['model']}\n\n"
        f"{chunks[page - 1]}"
    )


async def _invoke_codex(
    method: Callable[..., Any],
    *args: Any,
    timeout: float = 30.0,
    **kwargs: Any,
) -> Any:
    """Run sync app-server clients away from the QQ event loop and accept test fakes."""

    if inspect.iscoroutinefunction(method):
        return await asyncio.wait_for(method(*args, **kwargs), timeout=timeout)
    result = await asyncio.wait_for(asyncio.to_thread(method, *args, **kwargs), timeout=timeout)
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=timeout)
    return result


async def _invoke_account_mutation(
    method: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a credential mutation without reporting a misleading timeout.

    Cancelling ``asyncio.to_thread`` only stops waiting for the worker; it
    cannot stop a filesystem write already in progress.  Account mutations
    therefore run off the event loop but are awaited to their real outcome.
    """

    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class CommandService:
    def __init__(
        self,
        store: Store,
        *,
        codex_client: Any | None = None,
        account_manager: Any | None = None,
        codex_timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.codex_client = codex_client or CodexAppServerClient()
        self.account_manager = account_manager or CodexAccountManager()
        self.codex_timeout = max(float(codex_timeout), 0.1)

    async def _usage_text(self) -> str:
        try:
            account = parse_account_result(
                await _invoke_codex(
                    self.codex_client.read_account,
                    timeout=self.codex_timeout,
                )
            )
            if not account.is_authenticated or _is_api_key_account(account):
                return (
                    "Codex 当前未登录或使用 API key，app-server 无法读取限额。\n"
                    f"{usage_dashboard_hint()}"
                )
            payload = await _invoke_codex(
                self.codex_client.read_rate_limits,
                timeout=self.codex_timeout,
            )
            return format_usage_text(parse_rate_limits(payload))
        except Exception as exc:
            return _codex_failure_text("用量读取", exc, dashboard=True)

    async def _account_text(self) -> str:
        try:
            account = parse_account_result(
                await _invoke_codex(
                    self.codex_client.read_account,
                    timeout=self.codex_timeout,
                )
            )
            return _account_text(account)
        except Exception as exc:
            return _codex_failure_text("账号读取", exc)

    async def shutdown(self) -> None:
        """No-op; kept for API compatibility with the daemon shutdown path."""

        return

    async def handle(
        self,
        *,
        openid: str,
        message_id: str,
        content: str,
        passive_send: PassiveSender,
        active_send: ActiveSender,
    ) -> str:
        if not self.store.remember_inbound(message_id):
            return "duplicate"

        command = " ".join((content or "").strip().split())
        bound = self.store.get_bound_openid()
        bind_match = re.fullmatch(r"/bind\s+([A-Za-z0-9-]+)", command, flags=re.IGNORECASE)
        if bind_match:
            if not self.store.consume_pairing(bind_match.group(1), openid):
                await passive_send(openid, "配对码无效或已过期，请在源码目录运行 .\\codexbot.cmd pair。", message_id, 1)
                return "bad_pairing"
            try:
                result = await active_send(openid, "CodexBot 主动通知测试成功。")
                if result is None:
                    raise TimeoutError("QQ API returned no response")
            except Exception:
                await passive_send(
                    openid,
                    "绑定已完成，但主动通知测试失败。请在 QQ 中开启“允许主动发送”，再用 /status 检查。",
                    message_id,
                    1,
                )
            else:
                await passive_send(openid, "绑定成功，主动通知能力正常。", message_id, 1)
            return "bound"

        if not bound:
            await passive_send(openid, "机器人尚未绑定。请在源码目录运行 .\\codexbot.cmd pair 后发送 /bind 配对码。", message_id, 1)
            return "unbound"

        if not hmac_equal(bound, openid):
            return "unauthorized"

        lower = command.casefold()
        if lower == "/status":
            response = _status_text(self.store)
        elif lower == "/usage":
            response = await self._usage_text()
        elif lower == "/account":
            response = await self._account_text()
        elif lower.startswith("/account "):
            response = await _account_switch_text(
                command[len("/account"):],
                account_manager=self.account_manager,
                timeout=self.codex_timeout,
            )
        elif lower == "/last" or lower.startswith("/last "):
            parsed = _last_arguments(command[5:])
            if parsed is None:
                response = "用法：/last、/last 页码、/last 项目 [页码]"
            else:
                project, page = parsed
                response = _last_text(self.store, page, project)
        elif lower == "/mute":
            self.store.set_muted(True)
            response = "主动通知已暂停；状态和最近回复仍会更新，不会补发静音期间的旧通知。"
        elif lower == "/unmute":
            self.store.set_muted(False)
            response = "主动通知已恢复，只推送之后的新事件。"
        elif lower == "/help":
            response = HELP_TEXT
        else:
            response = "未知命令。\n\n" + HELP_TEXT
        await passive_send(openid, response, message_id, 1)
        return "replied"


def _account_switch_error(prefix: str, exc: BaseException) -> str:
    detail = _safe_field(exc, fallback="请稍后重试", limit=220)
    response = f"{prefix}失败：{detail}"
    if "正在运行" in detail:
        response += (
            "\n请先运行 .\\codexbot.cmd start 保持机器人在线，"
            "再完全退出 Codex/ChatGPT 后重试。"
        )
    return response


async def _account_sources_text(account_manager: Any, *, timeout: float) -> str:
    try:
        store = await _invoke_codex(account_manager.load_store, timeout=timeout)
    except Exception as exc:
        return _account_switch_error("读取 codex_login 账号", exc)
    try:
        snapshots = await _invoke_codex(account_switch.list_accounts, timeout=timeout)
    except Exception as exc:
        return _account_switch_error("读取 CodexBot 加密快照", exc)

    lines: list[str] = []
    accounts = tuple(getattr(store, "accounts", ()) or ())
    active_id = getattr(store, "active_account_id", None)
    if accounts:
        lines.append("codex_login 已保存账号：")
        for index, account in enumerate(accounts, 1):
            markers: list[str] = []
            if account.id == active_id:
                markers.append("当前")
            if not account.is_ready:
                markers.append("登录过期")
            suffix = f"（{'、'.join(markers)}）" if markers else ""
            email = _safe_field(account.email, fallback="无邮箱")
            lines.append(f"{index}. {_safe_field(account.name)} · {email}{suffix}")

    if snapshots:
        if lines:
            lines.append("")
        lines.append("CodexBot 加密快照：")
        for snapshot in snapshots:
            email = _safe_field(snapshot.email, fallback="未识别邮箱")
            lines.append(f"• {_safe_field(snapshot.name)}（{email}）")

    if not lines:
        return (
            "还没有可切换的账号。\n"
            "可先在 codex_login 中添加账号，或用 /account save 名称保存当前登录。"
        )
    lines.append("")
    lines.append("切换：/account use 序号、名称、邮箱或账号 ID")
    return "\n".join(lines)


async def _account_switch_text(
    argument: str,
    *,
    account_manager: Any,
    timeout: float,
) -> str:
    """Handle /account save|list|use|delete for QQ."""

    tokens = argument.strip().split()
    action = tokens[0].casefold() if tokens else ""
    if action == "list":
        return await _account_sources_text(account_manager, timeout=timeout)
    if action == "save":
        if len(tokens) < 2:
            return "用法：/account save 名称"
        name = " ".join(tokens[1:])
        try:
            snapshot = await _invoke_account_mutation(
                account_switch.save_current_account,
                name,
            )
        except account_switch.AccountSwitchError as exc:
            return _account_switch_error("保存", exc)
        except Exception as exc:
            return _account_switch_error("保存", exc)
        email = _safe_field(snapshot.email, fallback="未识别邮箱")
        display_name = _safe_field(snapshot.name)
        return f"已保存当前账号为 {display_name}（{email}）。\n用 /account use {display_name} 可随时切换。"
    if action == "use":
        if len(tokens) < 2:
            return "用法：/account use 序号、名称、邮箱或账号 ID"
        selector = " ".join(tokens[1:])

        # Prefer the codex_login store. Resolve before switching so a local
        # DPAPI snapshot with the same display name cannot silently shadow it.
        external_error: CodexAccountError | None = None
        try:
            store = await _invoke_codex(account_manager.load_store, timeout=timeout)
        except Exception as exc:
            return _account_switch_error("读取 codex_login 账号", exc)
        if tuple(getattr(store, "accounts", ()) or ()):
            try:
                await _invoke_codex(account_manager.resolve_account, selector, timeout=timeout)
            except CodexAccountError as exc:
                external_error = exc
            except Exception as exc:
                return _account_switch_error("解析 codex_login 账号", exc)
            else:
                try:
                    account = await _invoke_account_mutation(
                        account_manager.switch_account,
                        selector,
                    )
                except Exception as exc:
                    return _account_switch_error("切换 codex_login 账号", exc)
                email = _safe_field(account.email, fallback="无邮箱")
                display_name = _safe_field(account.name)
                return (
                    f"已切换到 codex_login 账号 {display_name}（{email}）。\n"
                    "已更新 Codex 登录并同步当前账号；现在可以重新启动 Codex。"
                )

        try:
            snapshots = await _invoke_codex(account_switch.list_accounts, timeout=timeout)
        except Exception as exc:
            return _account_switch_error("读取 CodexBot 加密快照", exc)
        local = next(
            (item for item in snapshots if item.name.casefold() == selector.casefold()),
            None,
        )
        if local is None and external_error is not None:
            return _account_switch_error("切换 codex_login 账号", external_error)
        try:
            email, _ = await _invoke_account_mutation(
                account_switch.switch_account,
                selector,
            )
        except Exception as exc:
            return _account_switch_error("切换 CodexBot 加密快照", exc)
        detail = _safe_field(email, fallback="未知邮箱")
        display_name = _safe_field(selector)
        return (
            f"已切换到 CodexBot 加密快照 {display_name}（{detail}）。\n"
            "已更新 Codex 登录；现在可以重新启动 Codex。"
        )
    if action == "delete":
        if len(tokens) < 2:
            return "用法：/account delete 名称"
        name = " ".join(tokens[1:])
        try:
            await _invoke_account_mutation(account_switch.delete_account, name)
        except account_switch.AccountSwitchError as exc:
            return _account_switch_error("删除", exc)
        except Exception as exc:
            return _account_switch_error("删除", exc)
        return f"已删除账号 {_safe_field(name)}。"
    return (
        "用法：\n"
        "/account save 名称 - 保存当前账号\n"
        "/account list - 列出 codex_login 账号和加密快照\n"
        "/account use 序号/名称/邮箱/ID - 安全切换账号\n"
        "/account delete 名称 - 删除 CodexBot 加密快照"
    )


def _status_text(store: Store) -> str:
    sessions = store.get_sessions_for_status()
    if not sessions:
        return "当前还没有收到 Codex 任务状态。"
    labels = {
        "idle": "空闲",
        "running": "处理中",
        "awaiting_approval": "等待本机审批",
        "completed": "已完成",
        "closed": "已关闭",
    }
    lines = [f"CodexBot：{'已静音' if store.is_muted() else '通知开启'}"]
    for session in sessions:
        updated = datetime.fromtimestamp(float(session["updated_at"])).strftime("%m-%d %H:%M:%S")
        lines.extend(
            [
                "",
                f"项目：{session['project']}",
                f"模型：{session['model']}",
                f"状态：{labels.get(str(session['status']), session['status'])}",
                f"更新：{updated}",
            ]
        )
    return "\n".join(lines)


def hmac_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
