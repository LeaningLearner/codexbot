from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import re
from typing import Any, Awaitable, Callable

from .codex_login import (
    AccountInfo,
    AppServerError,
    AppServerRPCError,
    AppServerTimeout,
    CodexAppServerClient,
    CodexLoginService,
    DeviceLoginResult,
    LoginInProgress,
    parse_account_result,
    parse_device_login_result,
)
from .codex_accounts import (
    CodexAccount,
    CodexAccountError,
    CodexAccountManager,
    CodexUsageError,
    format_account_list,
    format_saved_account_text,
    format_saved_usage_text,
)
from .codex_usage import format_usage_text, parse_rate_limits, usage_dashboard_hint
from .formatting import split_text
from .security import redact_secrets
from .store import Store


PassiveSender = Callable[[str, str, str, int], Awaitable[object]]
ActiveSender = Callable[[str, str], Awaitable[object]]


HELP_TEXT = (
    "CodexBot QQ 命令\n"
    "/bind 配对码 - 首次绑定或使用新配对码换绑\n"
    "/status - 查看 Codex 当前状态\n"
    "/last [项目] [页码] - 查看最近回复；只写页码时保持兼容\n"
    "/usage - 查看 Codex 各限额剩余百分比和重置时间\n"
    "/codex_account - 查看 Codex 邮箱、套餐和认证类型\n"
    "/codex_accounts - 列出 codex_login 保存的账号\n"
    "/codex_switch <序号|名称|ID> - 切换 codex_login 账号\n"
    "/codex_login - 启动 Codex 设备码登录/切换账号\n"
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
    if not account.is_authenticated or account.requires_openai_auth:
        return (
            "Codex 当前未登录（或当前认证方式不支持 OpenAI 账号读取）。\n"
            "认证类型：未登录\n"
            "请在 Codex 中登录后重试 /codex_account。"
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
    if isinstance(exc, LoginInProgress):
        return "已有 Codex 账号切换正在进行，请稍后重试。"
    if isinstance(exc, AppServerTimeout):
        text = f"Codex {action}超时，请稍后重试。"
    elif isinstance(exc, CodexUsageError) and exc.status == 401:
        text = "Codex 登录凭据已过期，请在 codex_login 中重新登录后重试。"
    elif isinstance(exc, CodexUsageError) and exc.status == 403:
        text = "Codex 用量接口暂时拒绝了请求，可能是网络安全校验，请稍后重试。"
    elif isinstance(exc, CodexUsageError):
        text = f"Codex {action}暂时失败，请稍后重试。"
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


async def _invoke_codex(method: Callable[..., Any], *args: Any, timeout: float = 30.0, **kwargs: Any) -> Any:
    """Run sync app-server clients away from the QQ event loop and accept test fakes."""

    if inspect.iscoroutinefunction(method):
        return await asyncio.wait_for(method(*args, **kwargs), timeout=timeout)
    result = await asyncio.wait_for(asyncio.to_thread(method, *args, **kwargs), timeout=timeout)
    if inspect.isawaitable(result):
        return await asyncio.wait_for(result, timeout=timeout)
    return result


def _login_completion_text(result: DeviceLoginResult) -> str:
    if result.completed:
        if result.account is not None:
            return (
                "Codex 设备码登录完成，账号已切换。\n"
                f"邮箱：{_safe_field(result.account.email)}"
            )
        return "Codex 设备码登录完成，账号已切换。"
    if result.cancelled:
        return "Codex 设备码登录已取消，登录进程已清理。"
    return f"Codex 设备码登录未完成，登录进程已清理：{_safe_field(result.error, fallback='请稍后重试。')}"


def _consume_completion_future(future: object) -> None:
    """Consume background delivery failures without leaking callback warnings."""

    try:
        result = getattr(future, "result", None)
        if result is not None:
            result()
    except Exception:
        # Login state and its app-server child are already finalized. A QQ
        # delivery failure must not resurrect the worker or leak its exception.
        pass


class CommandService:
    def __init__(
        self,
        store: Store,
        *,
        codex_client: Any | None = None,
        login_service: Any | None = None,
        account_manager: Any | None = None,
        codex_timeout: float = 30.0,
    ) -> None:
        self.store = store
        self._prefer_saved_accounts = codex_client is None or account_manager is not None
        self.codex_client = codex_client or CodexAppServerClient()
        self.account_manager = account_manager or CodexAccountManager()
        # A device-code flow intentionally keeps its own app-server child and
        # RPC lock for several minutes; it must not block /usage or /codex_account.
        self.login_service = login_service or CodexLoginService()
        self.codex_timeout = max(float(codex_timeout), 0.1)

    async def _usage_text(self) -> str:
        try:
            if self._prefer_saved_accounts:
                saved_account = await _invoke_codex(
                    self.account_manager.get_active_account,
                    timeout=self.codex_timeout,
                )
                if isinstance(saved_account, CodexAccount):
                    if not saved_account.is_chatgpt:
                        return (
                            "Codex 当前账号不是 ChatGPT OAuth 登录，无法直接读取限额。\n"
                            f"{usage_dashboard_hint()}"
                        )
                    payload = await _invoke_codex(
                        self.account_manager.read_usage,
                        saved_account,
                        timeout=self.codex_timeout,
                    )
                    return format_saved_usage_text(saved_account, payload)
            account = parse_account_result(
                await _invoke_codex(
                    self.codex_client.read_account,
                    timeout=self.codex_timeout,
                )
            )
            if (
                not account.is_authenticated
                or account.requires_openai_auth
                or _is_api_key_account(account)
            ):
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
            if self._prefer_saved_accounts:
                saved_account = await _invoke_codex(
                    self.account_manager.get_active_account,
                    timeout=self.codex_timeout,
                )
                if isinstance(saved_account, CodexAccount):
                    return format_saved_account_text(saved_account)
            account = parse_account_result(
                await _invoke_codex(
                    self.codex_client.read_account,
                    timeout=self.codex_timeout,
                )
            )
            return _account_text(account)
        except Exception as exc:
            return _codex_failure_text("账号读取", exc)

    async def _accounts_text(self) -> str:
        try:
            manager = self.account_manager
            accounts = await _invoke_codex(manager.list_accounts, timeout=self.codex_timeout)
            store = await _invoke_codex(manager.load_store, timeout=self.codex_timeout)
            return format_account_list(tuple(accounts), store.active_account_id)
        except Exception as exc:
            detail = redact_secrets(str(exc)).replace("\r", " ").replace("\n", " ")
            detail = " ".join(detail.split())[:180]
            return f"Codex 账号列表读取失败：{detail or '请稍后重试。'}"

    async def _switch_text(self, selector: str) -> str:
        try:
            account = await _invoke_codex(
                self.account_manager.switch_account,
                selector,
                timeout=self.codex_timeout,
            )
            return (
                "Codex 账号已切换。\n"
                f"名称：{_safe_field(account.name)}\n"
                f"邮箱：{_safe_field(account.email)}\n"
                "已更新本机 Codex auth.json；已打开的 Codex 窗口请重启后生效。"
            )
        except CodexAccountError as exc:
            detail = redact_secrets(str(exc)).replace("\r", " ").replace("\n", " ")
            detail = " ".join(detail.split())[:220]
            return f"Codex 账号切换失败：{detail or '请稍后重试。'}"
        except Exception:
            return "Codex 账号切换失败，请稍后重试。"

    async def _login_text(
        self,
        *,
        openid: str,
        active_send: ActiveSender,
    ) -> str:
        loop = asyncio.get_running_loop()

        def on_complete(result: DeviceLoginResult) -> None:
            async def deliver() -> None:
                await active_send(openid, _login_completion_text(result))

            if loop.is_closed():
                return
            coroutine = deliver()
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                future.add_done_callback(_consume_completion_future)
            except RuntimeError:
                # The event loop may be shutting down; the login worker still
                # cleans its process and lock independently.
                coroutine.close()

        started = False
        try:
            start_method = self.login_service.start_device_login
            try:
                start = await _invoke_codex(
                    start_method,
                    on_complete=on_complete,
                    timeout=self.codex_timeout,
                )
            except TypeError as exc:
                # Keep lightweight pre-existing test doubles compatible while
                # real CodexLoginService always receives the callback.
                if "on_complete" not in str(exc):
                    raise
                start = await _invoke_codex(
                    start_method,
                    timeout=self.codex_timeout,
                )
            started = True
            device = parse_device_login_result(start)
            return (
                "Codex 设备码登录已启动。请在浏览器打开以下地址并输入验证码；完成后会自动切换账号并回报。\n"
                f"verificationUrl: {device.verification_url}\n"
                f"userCode: {device.user_code}"
            )
        except Exception as exc:
            # If formatting/validation fails after the service created a live
            # session, release it rather than leaving an invisible login lock.
            if started:
                cancel_method = getattr(self.login_service, "cancel_device_login", None)
                if cancel_method is not None:
                    try:
                        await _invoke_codex(cancel_method, timeout=self.codex_timeout)
                    except Exception:
                        pass
            return _codex_failure_text("设备码登录", exc)

    async def shutdown(self) -> None:
        """Cancel a pending device login and wait briefly for child cleanup."""

        close_method = getattr(self.login_service, "close", None)
        cancel_method = getattr(self.login_service, "cancel_device_login", None)
        method = close_method or cancel_method
        if method is None:
            return
        try:
            await _invoke_codex(method, timeout=min(self.codex_timeout, 5.0))
        except Exception:
            # Daemon shutdown must continue even if an already-dying app-server
            # process does not acknowledge termination in time.
            pass

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
        elif lower == "/codex_account":
            response = await self._account_text()
        elif lower == "/codex_accounts":
            response = await self._accounts_text()
        elif lower == "/codex_switch" or lower.startswith("/codex_switch "):
            selector = command[len("/codex_switch") :].strip()
            response = "用法：/codex_switch 序号、名称或账号 ID" if not selector else await self._switch_text(selector)
        elif lower == "/codex_login":
            response = await self._login_text(
                openid=openid,
                active_send=active_send,
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
