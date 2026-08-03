from __future__ import annotations

from pathlib import Path

import pytest

from codexbot.commands import CommandService
from codexbot.store import Store


class SendRecorder:
    def __init__(self) -> None:
        self.passive: list[tuple[str, str, str, int]] = []
        self.active: list[tuple[str, str]] = []
        self.fail_active = False

    async def passive_send(self, openid: str, text: str, message_id: str, sequence: int) -> object:
        self.passive.append((openid, text, message_id, sequence))
        return object()

    async def active_send(self, openid: str, text: str) -> object:
        self.active.append((openid, text))
        if self.fail_active:
            raise ConnectionError("disabled")
        return object()


def _event(name: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": r"D:\projects\codexbot",
        "model": "gpt-5.6-codex",
    }
    payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_bind_dedupe_authorization_and_repair(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    service = CommandService(store)
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)

    outcome = await service.handle(
        openid="first-user",
        message_id="message-1",
        content="/bind abcd-ef23",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    )
    assert outcome == "bound"
    assert store.get_bound_openid() == "first-user"
    assert len(sent.passive) == 1
    assert "主动通知能力正常" in sent.passive[0][1]
    assert len(sent.active) == 1

    duplicate = await service.handle(
        openid="first-user",
        message_id="message-1",
        content="/status",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    )
    assert duplicate == "duplicate"

    unauthorized = await service.handle(
        openid="second-user",
        message_id="message-2",
        content="/status",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    )
    assert unauthorized == "unauthorized"
    assert len(sent.passive) == 1

    store.create_pairing("WXYZ-2345", expires_at=10_000_000_000.0)
    rebound = await service.handle(
        openid="second-user",
        message_id="message-3",
        content="/bind WXYZ-2345",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    )
    assert rebound == "bound"
    assert store.get_bound_openid() == "second-user"


@pytest.mark.asyncio
async def test_status_last_mute_and_unmute(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    service = CommandService(store)
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)

    store.ingest_hook(_event("UserPromptSubmit", prompt="开始任务"))
    store.ingest_hook(_event("Stop", last_assistant_message="完整回答🙂\n" * 300))

    for index, command in enumerate(("/status", "/last", "/last 999", "/mute", "/unmute"), 1):
        assert await service.handle(
            openid="owner",
            message_id=f"cmd-{index}",
            content=command,
            passive_send=sent.passive_send,
            active_send=sent.active_send,
        ) == "replied"

    responses = [entry[1] for entry in sent.passive]
    assert "项目：codexbot" in responses[0]
    assert "完整回答" in responses[1]
    assert "页码无效" in responses[2]
    assert store.is_muted() is False


@pytest.mark.asyncio
async def test_binding_survives_failed_proactive_test(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    service = CommandService(store)
    sent = SendRecorder()
    sent.fail_active = True
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)

    assert await service.handle(
        openid="owner",
        message_id="bind-failure",
        content="/bind ABCD-EF23",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "bound"
    assert store.get_bound_openid() == "owner"
    assert [message[3] for message in sent.passive] == [1]
    assert "主动通知测试失败" in sent.passive[-1][1]
