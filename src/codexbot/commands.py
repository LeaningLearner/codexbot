from __future__ import annotations

from datetime import datetime
import re
from typing import Awaitable, Callable

from .formatting import split_text
from .store import Store


PassiveSender = Callable[[str, str, str, int], Awaitable[object]]
ActiveSender = Callable[[str, str], Awaitable[object]]


HELP_TEXT = (
    "CodexBot QQ 命令\n"
    "/bind 配对码 - 首次绑定或使用新配对码换绑\n"
    "/status - 查看 Codex 当前状态\n"
    "/last [页码] - 查看最近一次完整回复\n"
    "/mute - 暂停主动通知\n"
    "/unmute - 恢复主动通知\n"
    "/help - 显示此帮助"
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


def _last_text(store: Store, page: int) -> str:
    reply = store.get_last_reply()
    if not reply:
        return "还没有可读取的 Codex 最终回复。"
    chunks = split_text(str(reply["content"]), limit=1000)
    if page < 1 or page > len(chunks):
        return f"页码无效，可用范围：1-{len(chunks)}。"
    return (
        f"最近一次 Codex 回复 [{page}/{len(chunks)}]\n"
        f"项目：{reply['project']}\n"
        f"模型：{reply['model']}\n\n"
        f"{chunks[page - 1]}"
    )


class CommandService:
    def __init__(self, store: Store) -> None:
        self.store = store

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
        elif lower.startswith("/last"):
            match = re.fullmatch(r"/last(?:\s+(\d+))?", lower)
            if not match:
                response = "用法：/last 或 /last 页码"
            else:
                response = _last_text(self.store, int(match.group(1) or "1"))
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


def hmac_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
