from __future__ import annotations

import asyncio
from datetime import datetime
import random
import time
from typing import Awaitable, Callable

from .formatting import bisect_segment, render_segment, split_text
from .security import redact_secrets
from .store import OutboxItem, Store


AsyncSender = Callable[[str, str], Awaitable[object]]


LENGTH_MARKERS = ("40054007", "40054018", "长度超限", "消息过长")
DUPE_MARKERS = ("40054005", "消息被去重")
PERMANENT_MARKERS = (
    "22006",
    "304061",
    "40034006",
    "40054013",
    "40034105",
    "40054004",
    "消息内容违规",
    "消息内容无效",
    "拒收",
    "无权限",
    "无好友关系",
)
RATE_MARKERS = ("40034100", "频控", "发送频率", "too many requests", "rate limit")


class RateLimiter:
    def __init__(self, per_minute: int = 18) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self.minimum_interval = 60.0 / per_minute
        self._last_sent = 0.0

    async def wait(self) -> None:
        delay = self.minimum_interval - (time.monotonic() - self._last_sent)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last_sent = time.monotonic()


def classify_delivery_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
    text = f"{code or ''} {status or ''} {exc}".casefold()
    if any(marker.casefold() in text for marker in LENGTH_MARKERS):
        return "length"
    if any(marker.casefold() in text for marker in DUPE_MARKERS):
        return "duplicate"
    if any(marker.casefold() in text for marker in PERMANENT_MARKERS):
        return "permanent"
    if any(marker.casefold() in text for marker in RATE_MARKERS):
        return "rate"
    return "transient"


def notification_text(item: OutboxItem) -> str:
    payload = item.payload
    stamp = datetime.fromtimestamp(float(payload.get("created_at", item.created_at))).strftime("%H:%M:%S")
    if item.kind == "task_started":
        return (
            "🚀 Codex 开始处理\n"
            f"项目：{payload.get('project', 'unknown')}\n"
            f"模型：{payload.get('model', 'unknown')}\n"
            f"时间：{stamp}\n"
            f"任务：{payload.get('preview') or '（无文本预览）'}"
        )
    if item.kind == "permission_required":
        return (
            "⏸ Codex 等待本机审批\n"
            f"项目：{payload.get('project', 'unknown')}\n"
            f"工具：{payload.get('tool', 'unknown')}\n"
            f"原因：{payload.get('reason', '需要确认的操作')}\n"
            "请返回 Codex 完成审批。"
        )
    if item.kind == "final_reply":
        return (
            "✅ Codex 已完成\n"
            f"项目：{payload.get('project', 'unknown')}\n"
            f"模型：{payload.get('model', 'unknown')}\n\n"
            f"{payload.get('content', '')}"
        )
    raise ValueError(f"unknown outbox kind: {item.kind}")


async def deliver_item(
    store: Store,
    item: OutboxItem,
    openid: str,
    sender: AsyncSender,
    limiter: RateLimiter,
) -> str:
    if store.is_muted():
        store.mark_outbox(item.id, "suppressed", "notifications muted")
        return "suppressed"

    segments = item.segments
    index = item.segment_index
    if segments is None:
        segments = split_text(notification_text(item))
        store.prepare_segments(item.id, segments)
        index = 0

    if index >= len(segments):
        store.mark_outbox(item.id, "delivered", "already complete")
        return "delivered"

    rendered = render_segment(segments, index)
    try:
        await limiter.wait()
        result = await sender(openid, rendered)
        if result is None:
            raise TimeoutError("QQ API returned no response")
    except Exception as exc:
        category = classify_delivery_error(exc)
        safe_error = redact_secrets(str(exc))
        if category == "length":
            if len(segments[index]) > 1:
                left, right = bisect_segment(segments[index])
                replacement = [*segments[:index], left, right, *segments[index + 1 :]]
                store.replace_segments(item.id, replacement, index)
                return "split"
            store.mark_outbox(item.id, "failed_permanent", safe_error)
            return "failed_permanent"
        if category == "duplicate":
            store.advance_segment(item.id, index, len(segments))
            return "delivered" if index + 1 >= len(segments) else "advanced"
        if category == "permanent":
            store.mark_outbox(item.id, "failed_permanent", safe_error)
            return "failed_permanent"
        if category == "rate":
            delay = 65.0
        else:
            delay = min(300.0, 5.0 * (2 ** min(item.attempts, 6))) + random.uniform(0.0, 2.0)
        store.reschedule(item.id, delay=delay, error=safe_error)
        return "retry"

    store.advance_segment(item.id, index, len(segments))
    return "delivered" if index + 1 >= len(segments) else "advanced"
