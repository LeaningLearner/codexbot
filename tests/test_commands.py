from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codexbot.codex_login import AccountInfo
from codexbot.commands import CommandService
from codexbot.codex_usage import parse_rate_limits
from codexbot.store import Store
from codexbot import account_switch


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
        content="/account",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "replied"
    assert "owner@example.com" in sent.passive[-1][1]
    assert "ChatGPT 登录" in sent.passive[-1][1]


@pytest.mark.asyncio
async def test_account_command_shows_real_login_even_with_requires_openai_auth(tmp_path: Path) -> None:
    """A provider that requires OpenAI auth must not hide an existing login."""
    store = Store(tmp_path / "state.sqlite3")
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)

    class OpenaiAuthClient(FakeCodexClient):
        def read_account(self) -> AccountInfo:
            return AccountInfo(
                "ikaabashidze8776@gmail.com",
                "plus",
                "chatgpt",
                requires_openai_auth=True,
            )

    service = CommandService(store, codex_client=OpenaiAuthClient())
    assert await service.handle(
        openid="owner",
        message_id="account-command",
        content="/account",
        passive_send=sent.passive_send,
        active_send=sent.active_send,
    ) == "replied"
    assert "ikaabashidze8776@gmail.com" in sent.passive[-1][1]
    assert "未登录" not in sent.passive[-1][1]


@pytest.mark.asyncio
async def test_account_command_list_use_delete(monkeypatch, tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    sent = SendRecorder()
    store.create_pairing("ABCD-EF23", expires_at=10_000_000_000.0)
    assert store.consume_pairing("ABCD-EF23", "owner", now=1.0)

    def _accounts_dir() -> Path:
        return tmp_path / "accounts"

    monkeypatch.setattr(account_switch, "accounts_dir", _accounts_dir)
    monkeypatch.setattr(account_switch, "auth_file_path", lambda: tmp_path / ".codex" / "auth.json")

    # list with no accounts
    service = CommandService(store, codex_client=FakeCodexClient())
    assert await service.handle(
        openid="owner", message_id="m1", content="/account list",
        passive_send=sent.passive_send, active_send=sent.active_send,
    ) == "replied"
    assert "还没有保存任何账号" in sent.passive[-1][1]

    # /account with no args shows the current account
    assert await service.handle(
        openid="owner", message_id="m2", content="/account",
        passive_send=sent.passive_send, active_send=sent.active_send,
    ) == "replied"
    assert "owner@example.com" in sent.passive[-1][1]

    # unknown subcommand shows usage
    assert await service.handle(
        openid="owner", message_id="m3", content="/account frobnicate",
        passive_send=sent.passive_send, active_send=sent.active_send,
    ) == "replied"
    assert "/account save 名称" in sent.passive[-1][1]
