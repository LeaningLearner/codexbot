from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from codexbot.processes import HostProcess
from codexbot.store import (
    LAST_REPLY_TTL_ENV,
    OUTBOX_TTL_ENV,
    PERMISSION_NOTIFICATION_DELAY,
    PERMISSION_NOTIFICATION_ENV,
    Store,
)


def _rows(path: Path, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def _event(name: str, **extra: object) -> dict[str, object]:
    event: dict[str, object] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "cwd": r"D:\work\示例项目",
        "model": "gpt-5.6-codex",
    }
    event.update(extra)
    return event


def test_hook_ingestion_redacts_deduplicates_and_keeps_full_final_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.sqlite3"
    store = Store(database)
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    full_prompt = "请检查 api_key=never-write-this " + "中文任务 " * 40
    host = HostProcess(123, 456.5, "desktop")
    start = _event("UserPromptSubmit", prompt=full_prompt)

    assert store.ingest_hook(start, host) is True
    assert store.ingest_hook(start, host) is False

    outbox = _rows(database, "SELECT * FROM outbox ORDER BY id")
    assert len(outbox) == 1
    start_payload = json.loads(outbox[0]["payload_json"])
    assert start_payload["project"] == "示例项目"
    assert "never-write-this" not in start_payload["preview"]
    assert len(start_payload["preview"]) <= 120
    assert store.list_hosts() == [host]

    permission = _event(
        "PermissionRequest",
        tool_name="shell_command",
        tool_input={"command": "deploy --token=approval-secret", "irrelevant": full_prompt},
    )
    assert store.ingest_hook(permission) is True
    permission_payload = json.loads(
        _rows(database, "SELECT payload_json FROM outbox WHERE kind = 'permission_required'")[0][0]
    )
    assert permission_payload["tool"] == "shell_command"
    assert "approval-secret" not in permission_payload["reason"]
    assert "irrelevant" not in permission_payload["reason"]

    final = "最终回复第一行\n\n完整内容🙂"
    assert store.ingest_hook(_event("Stop", last_assistant_message=final)) is True
    assert store.get_last_reply()["content"] == final  # type: ignore[index]

    disk_text = "".join(
        path.read_bytes().decode("utf-8", errors="ignore")
        for path in tmp_path.iterdir()
        if path.is_file()
    )
    assert full_prompt not in disk_text
    assert "never-write-this" not in disk_text
    assert "approval-secret" not in disk_text


def test_store_explicitly_closes_sqlite_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = Store(tmp_path / "state.sqlite3")
    opened: list[sqlite3.Connection] = []
    original_connect = Store._connect

    def tracked_connect(current: Store) -> sqlite3.Connection:
        connection = original_connect(current)
        opened.append(connection)
        return connection

    monkeypatch.setattr(Store, "_connect", tracked_connect)
    store.set_setting("connection-test", "closed-after-use")
    assert store.get_setting("connection-test") == "closed-after-use"

    assert opened
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_full_replies_and_pending_outbox_items_expire(tmp_path: Path) -> None:
    store = Store(
        tmp_path / "state.sqlite3",
        last_reply_ttl=60.0,
        outbox_ttl=60.0,
    )
    final = "full reply retained for /last only during the privacy window"

    assert store.ingest_hook(_event("Stop", last_assistant_message=final))
    reply = store.get_last_reply()
    assert reply is not None
    created_at = float(reply["created_at"])
    assert store.get_last_reply(now=created_at + 59.0)["content"] == final  # type: ignore[index]
    assert store.get_due_outbox(now=created_at + 59.0) is not None

    store.cleanup(now=created_at + 61.0)

    assert store.get_last_reply(now=created_at + 61.0) is None
    assert _rows(store.path, "SELECT COUNT(*) AS count FROM outbox")[0]["count"] == 0


def test_content_ttls_are_configurable_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LAST_REPLY_TTL_ENV, "12.5")
    monkeypatch.setenv(OUTBOX_TTL_ENV, "23.5")

    store = Store(tmp_path / "state.sqlite3")

    assert store.last_reply_ttl == 12.5
    assert store.outbox_ttl == 23.5


def test_same_session_id_in_different_projects_stays_isolated(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    first = _event(
        "UserPromptSubmit",
        session_id="reused-session",
        turn_id="reused-turn",
        cwd=r"D:\work\project-one",
        prompt="same prompt",
    )
    second = _event(
        "UserPromptSubmit",
        session_id="reused-session",
        turn_id="reused-turn",
        cwd=r"E:\work\project-two",
        prompt="same prompt",
    )

    assert store.ingest_hook(first) is True
    assert store.ingest_hook(second) is True

    sessions = _rows(
        store.path,
        "SELECT session_id, cwd, project FROM sessions ORDER BY cwd",
    )
    assert len(sessions) == 2
    assert [row["project"] for row in sessions] == ["project-one", "project-two"]
    assert sessions[0]["session_id"] != sessions[1]["session_id"]

    outbox = _rows(store.path, "SELECT session_id FROM outbox ORDER BY id")
    assert len(outbox) == 2
    assert outbox[0]["session_id"] != outbox[1]["session_id"]


def test_permission_resolution_is_scoped_to_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    store = Store(tmp_path / "state.sqlite3")
    common = {
        "session_id": "reused-session",
        "turn_id": "reused-turn",
        "tool_use_id": "reused-call",
        "tool_name": "shell_command",
        "tool_input": {"description": "same operation"},
    }

    assert store.ingest_hook(_event("PermissionRequest", cwd=r"D:\work\project-one", **common))
    assert store.ingest_hook(_event("PermissionRequest", cwd=r"E:\work\project-two", **common))
    assert not store.ingest_hook(
        _event(
            "PostToolUse",
            cwd=r"E:\work\project-two",
            session_id="reused-session",
            turn_id="reused-turn",
            tool_use_id="reused-call",
            tool_name="shell_command",
        )
    )

    states = _rows(store.path, "SELECT state FROM outbox ORDER BY id")
    assert [row["state"] for row in states] == ["pending", "suppressed"]


def test_existing_session_state_is_migrated_and_still_deduplicates(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    event = _event(
        "UserPromptSubmit",
        session_id="legacy-session",
        cwd=r"D:\work\legacy-project",
        prompt="legacy prompt",
    )
    store = Store(database)
    assert store.ingest_hook(event)
    legacy_event_key = Store._event_key(event)

    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE sessions SET session_id = 'legacy-session'")
        connection.execute(
            "UPDATE outbox SET session_id = 'legacy-session', event_key = ?",
            (legacy_event_key,),
        )
        connection.execute(
            "DELETE FROM settings WHERE key = 'session_scope_version'"
        )

    migrated = Store(database)
    assert not migrated.ingest_hook(event)
    sessions = _rows(database, "SELECT session_id FROM sessions")
    assert len(sessions) == 1
    assert str(sessions[0]["session_id"]).startswith("v2:")
    assert _rows(database, "SELECT COUNT(*) AS count FROM outbox")[0]["count"] == 1


def test_legacy_singleton_last_reply_is_migrated_before_indexes(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE last_reply (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                session_id TEXT NOT NULL,
                turn_id TEXT,
                project TEXT NOT NULL,
                model TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO last_reply(singleton, session_id, turn_id, project, model, content, created_at)
            VALUES (1, 'legacy-session', 'legacy-turn', 'legacy-project', 'legacy-model', 'preserved reply', 100)
            """
        )

    store = Store(database)

    reply = store.get_last_reply(now=100)
    assert reply is not None
    assert reply["content"] == "preserved reply"
    columns = [row["name"] for row in _rows(database, "PRAGMA table_info(last_reply)")]
    assert "singleton" not in columns
    assert "reply_id" in columns
    assert _rows(database, "SELECT COUNT(*) AS count FROM last_reply")[0]["count"] == 1


def test_last_reply_history_is_bounded_per_session_and_selectable_by_project(tmp_path: Path) -> None:
    store = Store(tmp_path / "history.sqlite3", last_reply_limit=2)
    for index in range(3):
        assert store.ingest_hook(
            _event(
                "Stop",
                turn_id=f"turn-{index}",
                cwd=r"D:\work\history-project",
                last_assistant_message=f"reply-{index}",
            )
        )

    replies = store.get_last_replies(project="history-project")
    assert [reply["content"] for reply in replies] == ["reply-2", "reply-1"]
    assert store.get_last_reply(project="history-project")["content"] == "reply-2"  # type: ignore[index]


def test_pairing_expiry_binding_and_prebinding_suppression(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(_event("UserPromptSubmit", prompt="任务"))
    now = time.time()
    store.create_pairing("ABCD-EF23", expires_at=now + 100.0)

    assert store.consume_pairing("abcd ef23", "openid-1", now=now)
    assert store.get_bound_openid() == "openid-1"
    assert store.get_due_outbox(now=now + 1.0) is None

    state = _rows(store.path, "SELECT state FROM outbox")[0]["state"]
    assert state == "suppressed"
    assert not store.consume_pairing("ABCD-EF23", "openid-2", now=now)

    store.create_pairing("WXYZ-2345", expires_at=now + 200.0)
    assert not store.consume_pairing("WXYZ-2345", "openid-2", now=now + 201.0)
    assert store.get_bound_openid() == "openid-1"


def test_muted_events_are_suppressed_at_ingestion_and_never_backfilled(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.set_muted(True)
    assert store.ingest_hook(_event("UserPromptSubmit", prompt="静音任务"))
    assert store.get_due_outbox() is None

    store.set_muted(False)
    assert store.get_due_outbox() is None
    assert store.ingest_hook(_event("UserPromptSubmit", turn_id="turn-2", prompt="恢复后的任务"))
    assert store.get_due_outbox() is not None


def test_distinct_permission_tool_calls_are_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    base = {
        "tool_name": "shell_command",
        "tool_input": {"command": "same-command"},
    }

    assert store.ingest_hook(_event("PermissionRequest", tool_use_id="call-1", **base))
    assert store.ingest_hook(_event("PermissionRequest", tool_use_id="call-2", **base))
    assert not store.ingest_hook(_event("PermissionRequest", tool_use_id="call-2", **base))

    count = _rows(store.path, "SELECT COUNT(*) AS count FROM outbox")[0]["count"]
    assert count == 2


def test_permission_notification_is_suppressed_when_tool_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "state.sqlite3")
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    permission = _event(
        "PermissionRequest",
        tool_use_id="call-1",
        tool_name="shell_command",
        tool_input={"description": "自动审查中的操作"},
    )
    assert store.ingest_hook(permission)
    assert store.get_due_outbox() is None

    post_tool = _event(
        "PostToolUse",
        tool_use_id="call-1",
        tool_name="shell_command",
        tool_response={"exit_code": 0},
    )
    assert store.ingest_hook(post_tool) is False
    row = _rows(store.path, "SELECT state, last_error FROM outbox")[0]
    assert row["state"] == "suppressed"
    assert "resolved" in row["last_error"]
    assert store.get_due_outbox(now=time.time() + PERMISSION_NOTIFICATION_DELAY + 1) is None


def test_permission_requests_are_quiet_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PERMISSION_NOTIFICATION_ENV, raising=False)
    store = Store(tmp_path / "default.sqlite3")
    assert store.ingest_hook(
        _event(
            "PermissionRequest",
            tool_name="shell_command",
            tool_input={"description": "auto-review should not notify QQ"},
        )
    ) is False
    assert _rows(store.path, "SELECT COUNT(*) AS count FROM outbox")[0]["count"] == 0


def test_non_interactive_permission_modes_do_not_create_waiting_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    for mode in ("dontAsk", "bypassPermissions"):
        store = Store(tmp_path / f"{mode}.sqlite3")
        assert store.ingest_hook(
            _event(
                "PermissionRequest",
                permission_mode=mode,
                tool_name="shell_command",
                tool_input={"description": "不应发出等待通知"},
            )
        ) is False
        assert _rows(store.path, "SELECT COUNT(*) AS count FROM outbox")[0]["count"] == 0
