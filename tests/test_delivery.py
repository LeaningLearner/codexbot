from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from codexbot.delivery import RateLimiter, classify_delivery_error, deliver_item, notification_text
from codexbot.store import Store


class NoWaitLimiter:
    async def wait(self) -> None:
        return None


def _event(name: str, turn: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": turn,
        "cwd": r"D:\projects\codexbot",
        "model": "gpt-5.6-codex",
    }
    payload.update(extra)
    return payload


def _outbox_row(store: Store) -> sqlite3.Row:
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("SELECT * FROM outbox ORDER BY id LIMIT 1").fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_event_order_and_exact_final_content(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(_event("UserPromptSubmit", "turn-1", prompt="开始"))
    store.ingest_hook(
        _event(
            "PermissionRequest",
            "turn-1",
            tool_name="shell_command",
            tool_input={"description": "需要安装依赖"},
        )
    )
    final = "第一段\n\n" + ("完整回复🙂\n" * 400)
    store.ingest_hook(_event("Stop", "turn-1", last_assistant_message=final))

    messages: list[str] = []

    async def sender(openid: str, text: str) -> object:
        assert openid == "owner"
        messages.append(text)
        return object()

    while (item := store.get_due_outbox()) is not None:
        await deliver_item(store, item, "owner", sender, NoWaitLimiter())  # type: ignore[arg-type]

    assert "开始处理" in messages[0]
    assert "等待本机审批" in messages[1]
    assert "Codex 已完成" in messages[2]
    assert len(messages) > 3

    final_messages = messages[2:]
    payloads = [message.split("\n", 1)[1] for message in final_messages]
    assert "".join(payloads).endswith(final)


@pytest.mark.asyncio
async def test_length_error_adaptively_bisects_then_completes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(_event("Stop", "turn-1", last_assistant_message="长回复🙂" * 100))
    original = store.get_due_outbox()
    assert original is not None
    expected = notification_text(original)

    class TooLong(Exception):
        code = 40054007

    calls = 0

    async def sender(_openid: str, _text: str) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TooLong("message length exceeded")
        return object()

    assert await deliver_item(store, original, "owner", sender, NoWaitLimiter()) == "split"  # type: ignore[arg-type]
    segments = json.loads(_outbox_row(store)["segments_json"])
    assert len(segments) == 2
    assert "".join(segments) == expected

    while (item := store.get_due_outbox()) is not None:
        await deliver_item(store, item, "owner", sender, NoWaitLimiter())  # type: ignore[arg-type]
    assert _outbox_row(store)["state"] == "delivered"


@pytest.mark.asyncio
async def test_transient_rate_and_permanent_errors_are_distinct(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("codexbot.delivery.random.uniform", lambda _a, _b: 0.0)

    cases = [
        (ConnectionError("network down"), "pending", "retry"),
        (RuntimeError("40034100 rate limit"), "pending", "retry"),
        (RuntimeError("40054005 消息被去重"), "delivered", "delivered"),
        (RuntimeError("40054013 user 拒收"), "failed_permanent", "failed_permanent"),
    ]
    for index, (error, expected_state, expected_outcome) in enumerate(cases):
        store = Store(tmp_path / f"state-{index}.sqlite3")
        store.ingest_hook(_event("UserPromptSubmit", f"turn-{index}", prompt="任务"))

        async def sender(_openid: str, _text: str, error: Exception = error) -> object:
            raise error

        item = store.get_due_outbox()
        assert item is not None
        assert await deliver_item(store, item, "owner", sender, NoWaitLimiter()) == expected_outcome  # type: ignore[arg-type]
        row = _outbox_row(store)
        assert row["state"] == expected_state
        if expected_state == "pending":
            assert row["attempts"] == 1
            assert row["next_attempt_at"] > 0

    assert classify_delivery_error(RuntimeError("40054007")) == "length"
    assert classify_delivery_error(RuntimeError("40054005 消息被去重")) == "duplicate"
    assert classify_delivery_error(RuntimeError("40034100")) == "rate"
    assert classify_delivery_error(RuntimeError("40054013")) == "permanent"
    assert classify_delivery_error(RuntimeError("40034006 消息内容违规")) == "permanent"


@pytest.mark.asyncio
async def test_rate_limiter_stays_below_twenty_messages_per_minute(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    clock = [100.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("codexbot.delivery.time.monotonic", monotonic)
    monkeypatch.setattr("codexbot.delivery.asyncio.sleep", sleep)
    limiter = RateLimiter(per_minute=18)

    await limiter.wait()
    await limiter.wait()
    await limiter.wait()

    assert len(sleeps) == 2
    assert all(delay >= 60 / 18 for delay in sleeps)


@pytest.mark.asyncio
async def test_unsplittable_length_error_stops_and_error_text_is_redacted(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(_event("UserPromptSubmit", "turn-1", prompt="任务"))
    item = store.get_due_outbox()
    assert item is not None
    store.prepare_segments(item.id, ["x"])
    item = store.get_due_outbox()
    assert item is not None

    async def sender(_openid: str, _text: str) -> object:
        raise RuntimeError("40054007 token=must-not-persist")

    assert await deliver_item(store, item, "owner", sender, NoWaitLimiter()) == "failed_permanent"  # type: ignore[arg-type]
    row = _outbox_row(store)
    assert row["state"] == "failed_permanent"
    assert "must-not-persist" not in row["last_error"]
