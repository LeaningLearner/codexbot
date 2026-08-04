from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from collections.abc import Mapping
from typing import Any

from .codex_login import APP_SERVER_DASHBOARD_URL


@dataclass(frozen=True)
class RateLimitBucket:
    name: str
    used_percent: float
    window_duration_minutes: float | None
    resets_at: float | None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class RateLimitSnapshot:
    buckets: tuple[RateLimitBucket, ...]


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bucket(value: object, name: str) -> RateLimitBucket | None:
    raw = _as_mapping(value)
    used = _number(raw.get("usedPercent"))
    if used is None:
        used = _number(raw.get("used_percent"))
    if used is None:
        return None
    duration = _number(raw.get("windowDurationMins"))
    if duration is None:
        seconds = _number(raw.get("windowDurationSeconds"))
        if seconds is None:
            seconds = _number(raw.get("limit_window_seconds"))
        duration = seconds / 60.0 if seconds is not None else None
    reset = _number(raw.get("resetsAt"))
    if reset is None:
        reset = _number(raw.get("reset_at"))
    return RateLimitBucket(
        name=str(name),
        used_percent=max(0.0, min(100.0, used)),
        window_duration_minutes=duration,
        resets_at=reset,
    )


def _add_bucket(
    buckets: list[RateLimitBucket],
    seen: set[str],
    name: str,
    value: object,
) -> None:
    display_name = str(name)
    key = display_name.casefold()
    suffix = 2
    while key in seen:
        display_name = f"{name}#{suffix}"
        key = display_name.casefold()
        suffix += 1
    parsed = _bucket(value, display_name)
    if parsed is None:
        return
    seen.add(key)
    buckets.append(parsed)


def _snapshot_base(raw: Mapping[str, Any], fallback: str = "") -> str:
    value = raw.get("limitName") or raw.get("limitId") or fallback
    return str(value).strip() if value is not None else ""


def _snapshot_buckets(
    value: object,
    *,
    label: str,
    buckets: list[RateLimitBucket],
    seen: set[str],
) -> None:
    """Recursively parse a RateLimitSnapshot and its primary/secondary windows."""

    raw = _as_mapping(value)
    direct_name = label or _snapshot_base(raw) or "limit"
    _add_bucket(buckets, seen, direct_name, raw)
    for window_name in ("primary", "secondary"):
        child = raw.get(window_name)
        if not isinstance(child, Mapping):
            continue
        child_label = f"{direct_name}/{window_name}" if label or _snapshot_base(raw) else window_name
        _snapshot_buckets(
            child,
            label=child_label,
            buckets=buckets,
            seen=seen,
        )


def parse_rate_limits(payload: object) -> RateLimitSnapshot:
    """Parse both current app-server rate-limit layouts.

    Codex versions have returned either ``primary``/``secondary`` under a
    ``rateLimits`` object or arbitrary buckets under
    ``rateLimitsByLimitId``. Unknown fields are ignored so a newer server can
    add metadata without breaking QQ usage output.
    """

    root = _as_mapping(payload)
    buckets: list[RateLimitBucket] = []
    seen: set[str] = set()

    rate_limits = _as_mapping(root.get("rateLimits"))
    if rate_limits:
        _snapshot_buckets(
            rate_limits,
            label=_snapshot_base(rate_limits),
            buckets=buckets,
            seen=seen,
        )

    by_limit_id = _as_mapping(root.get("rateLimitsByLimitId"))
    if not by_limit_id:
        by_limit_id = _as_mapping(rate_limits.get("rateLimitsByLimitId"))
    for name, value in by_limit_id.items():
        snapshot = _as_mapping(value)
        base = _snapshot_base(snapshot, str(name))
        _snapshot_buckets(
            snapshot,
            label=base,
            buckets=buckets,
            seen=seen,
        )

    # codex_login reads the ChatGPT backend directly. Its response uses the
    # snake_case ``rate_limit.primary_window``/``secondary_window`` shape,
    # rather than app-server's ``rateLimitsByLimitId`` shape.
    backend_limits = _as_mapping(root.get("rate_limit") or root.get("rateLimit"))
    for name, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        window = backend_limits.get(key)
        if not isinstance(window, Mapping):
            window = backend_limits.get(name)
        if isinstance(window, Mapping):
            _add_bucket(buckets, seen, name, window)

    # Be tolerant of a result that is itself the rate-limit object.
    if not buckets:
        for name, value in root.items():
            if name in {"rateLimits", "rateLimitsByLimitId"}:
                continue
            _snapshot_buckets(
                value,
                label=str(name),
                buckets=buckets,
                seen=seen,
            )

    return RateLimitSnapshot(tuple(buckets))


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _window_text(minutes: float | None) -> str:
    if minutes is None or not math.isfinite(minutes) or minutes <= 0:
        return "未知"
    if minutes >= 24 * 60 and minutes % (24 * 60) == 0:
        return f"{_number_text(minutes / (24 * 60))} 天"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{_number_text(minutes / 60)} 小时"
    return f"{_number_text(minutes)} 分钟"


def _reset_text(timestamp: float | None) -> str:
    if timestamp is None or not math.isfinite(timestamp):
        return "未知"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"


def format_usage_text(
    snapshot: RateLimitSnapshot | object,
    *,
    dashboard_url: str = APP_SERVER_DASHBOARD_URL,
) -> str:
    if not isinstance(snapshot, RateLimitSnapshot):
        snapshot = parse_rate_limits(snapshot)
    if not snapshot.buckets:
        return (
            "Codex 当前没有返回可用的限额 bucket；可能是旧版 Codex 或当前认证方式不支持读取用量。\n"
            f"可查看用量面板：{dashboard_url}"
        )

    lines = ["Codex 用量（剩余）"]
    for bucket in snapshot.buckets:
        lines.append(
            f"{bucket.name}：剩余 {_number_text(bucket.remaining_percent)}% · "
            f"窗口 {_window_text(bucket.window_duration_minutes)} · "
            f"重置 {_reset_text(bucket.resets_at)}"
        )
    lines.append(f"用量面板：{dashboard_url}")
    return "\n".join(lines)


def usage_dashboard_hint(*, dashboard_url: str = APP_SERVER_DASHBOARD_URL) -> str:
    return f"可查看用量面板：{dashboard_url}"
