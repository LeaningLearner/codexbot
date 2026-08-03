from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codexbot.codex_login import AccountInfo, DeviceLoginResult, DeviceLoginStart
from codexbot.commands import CommandService
from codexbot.codex_usage import parse_rate_limits
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


class FakeCodexClient:
    def read_account(self) -> AccountInfo:
        return AccountInfo("owner@example.com", "plus", "chatgpt")

    def read_rate_limits(self) -> dict[str, object]:
        return {
            "rateLimitsByLimitId": {
                "five_hour": {
                    "limitName": "5h",
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1_800_000_000},
                    "secondary": {"usedPercent": 50, "windowDurationMins": 10_080, "resetsAt": 1_800_100_000},
                }
            }
        }


class FakeLoginService:
    def __init__(self) -> None:
        self.callback = None
        self.cancelled = False

    def start_device_login(self, *, on_complete=None) -> DeviceLoginStart:
        self.callback = on_complete
        return DeviceLoginStart("login-1", "https://example.test/device", "ABCD-EFGH")

    def cancel_device_login(self) -> bool:
        self.cancelled = True
        return True


def test_usage_parser_keeps_primary_and_secondary_for_multiple_limit_ids() -> None:
    snapshot = parse_rate_limits(
        {
            "rateLimitsByLimitId": {
                "workspace": {
                    "limitName": "Workspace",
                    "limitId": "workspace",
                    "primary": {"usedPercent": 10},
                    "secondary": {"usedPercent": 20},
                },
                "personal": {
                    "limitId": "personal",
                    "primary": {"usedPercent": 30},
                    "secondary": {"usedPercent": 40},
                },
            }
        }
    )
    assert [bucket.name for bucket in snapshot.buckets] == [
        "Workspace/primary",
        "Workspace/secondary",
        "personal/primary",
        "personal/secondary",
    ]

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


@pytest.mark.asyncio
async def test_usage_and_account_commands_use_codex_app_server_data(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)
    service = CommandService(store, codex_client=FakeCodexClient())

    assert await service.handle(
        openid="owner",
        message_id="usage-command",
        content="/usage",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "replied"
    assert "5h/primary" in sent.passive[-1][1]
    assert "剩余 75%" in sent.passive[-1][1]

    assert await service.handle(
        openid="owner",
        message_id="account-command",
        content="/codex_account",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "replied"
    assert "owner@example.com" in sent.passive[-1][1]
    assert "ChatGPT 登录" in sent.passive[-1][1]


@pytest.mark.asyncio
async def test_device_login_completion_uses_active_sender_and_real_start_type(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)
    login = FakeLoginService()
    service = CommandService(store, login_service=login)

    assert await service.handle(
        openid="owner",
        message_id="login-command",
        content="/codex_login",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "replied"
    assert "verificationUrl: https://example.test/device" in sent.passive[-1][1]
    assert "userCode: ABCD-EFGH" in sent.passive[-1][1]
    assert sent.active == []
    assert login.callback is not None

    login.callback(
        DeviceLoginResult(
            started=DeviceLoginStart("login-1", "https://example.test/device", "ABCD-EFGH"),
            completed=True,
            account=AccountInfo("new@example.com", "plus", "chatgpt"),
        )
    )
    await asyncio.sleep(0.01)
    assert sent.active == [("owner", "Codex 设备码登录完成，账号已切换。\n邮箱：new@example.com")]
    assert all(len(message) == 2 for message in sent.active)


@pytest.mark.asyncio
async def test_command_service_shutdown_cancels_device_login(tmp_path: Path) -> None:
    login = FakeLoginService()
    service = CommandService(Store(tmp_path / "state.sqlite3"), login_service=login)

    await service.shutdown()

    assert login.cancelled is True
