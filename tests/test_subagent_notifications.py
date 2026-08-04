from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from codexbot.store import PERMISSION_NOTIFICATION_DELAY, PERMISSION_NOTIFICATION_ENV, Store


def _event(name: str, turn_id: str, **extra: object) -> dict[str, object]:
    event: dict[str, object] = {
        "hook_event_name": name,
        "session_id": "shared-parent-session",
        "turn_id": turn_id,
        "cwd": r"D:\work\project",
        "model": "gpt-5.6-luna",
    }
    event.update(extra)
    return event


def _rows(path: Path, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def _transcript(tmp_path: Path, name: str, *, subagent: bool) -> Path:
    path = tmp_path / f"{name}.jsonl"
    payload: dict[str, object] = {
        "id": name,
        "session_id": "shared-parent-session",
        "thread_source": "subagent" if subagent else "user",
        "source": {"subagent": {"thread_spawn": {"depth": 1}}}
        if subagent
        else "vscode",
    }
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n",
        encoding="utf-8",
    )
    return path


def test_subagent_turns_do_not_create_start_or_final_notifications(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    worker_one = _transcript(tmp_path, "worker-one", subagent=True)
    worker_two = _transcript(tmp_path, "worker-two", subagent=True)

    assert store.ingest_hook(
        _event("UserPromptSubmit", "root-turn", prompt="总任务")
    )
    assert not store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "worker-one",
            transcript_path=str(worker_one),
            prompt="子智能体任务一",
        )
    )
    assert not store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "worker-two",
            transcript_path=str(worker_two),
            prompt="子智能体任务二",
        )
    )
    assert not store.ingest_hook(
        _event(
            "Stop",
            "worker-one",
            transcript_path=str(worker_one),
            last_assistant_message="子智能体结果",
        )
    )

    session = _rows(
        store.path,
        "SELECT turn_id, status, prompt_preview FROM sessions",
    )[0]
    assert session["turn_id"] == "root-turn"
    assert session["status"] == "running"
    assert session["prompt_preview"] == "总任务"
    assert [row["kind"] for row in _rows(store.path, "SELECT kind FROM outbox")] == [
        "task_started"
    ]
    assert store.get_last_reply() is None

    assert store.ingest_hook(
        _event("Stop", "root-turn", last_assistant_message="总运行结果")
    )
    assert [
        row["kind"] for row in _rows(store.path, "SELECT kind FROM outbox ORDER BY id")
    ] == ["task_started", "final_reply"]
    assert store.get_last_reply()["content"] == "总运行结果"  # type: ignore[index]


def test_explicit_and_normalized_subagent_events_are_quiet(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")

    assert not store.ingest_hook(
        _event(
            "SubagentStart",
            "root-turn",
            agent_id="worker-id",
            agent_type="luna_worker",
        )
    )
    assert not store.ingest_hook(
        _event(
            "SubagentStop",
            "root-turn",
            agent_id="worker-id",
            agent_type="luna_worker",
            last_assistant_message="CHILD_FINAL",
        )
    )
    assert not store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "root-turn",
            agent_id="worker-id",
            agent_type="luna_worker",
            prompt="归一化子任务",
        )
    )
    assert not store.ingest_hook(
        _event(
            "Stop",
            "root-turn",
            agent_id="worker-id",
            agent_type="luna_worker",
            last_assistant_message="NORMALIZED_CHILD_FINAL",
        )
    )

    assert _rows(store.path, "SELECT kind FROM outbox") == []
    assert store.get_last_reply() is None


def test_subagent_permission_request_is_still_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PERMISSION_NOTIFICATION_ENV, "1")
    store = Store(tmp_path / "state.sqlite3")
    worker = _transcript(tmp_path, "worker", subagent=True)
    store.ingest_hook(_event("UserPromptSubmit", "root-turn", prompt="总任务"))
    store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "worker-turn",
            transcript_path=str(worker),
            prompt="子智能体任务",
        )
    )

    assert store.ingest_hook(
        _event(
            "PermissionRequest",
            "worker-turn",
            transcript_path=str(worker),
            tool_name="shell_command",
            tool_use_id="worker-approval",
            tool_input={"description": "需要用户确认"},
        )
    )

    permission = _rows(
        store.path,
        "SELECT turn_id, payload_json, state FROM outbox "
        "WHERE kind = 'permission_required'",
    )[0]
    assert permission["turn_id"] == "worker-turn"
    assert permission["state"] == "pending"
    assert json.loads(permission["payload_json"])["reason"] == "需要用户确认"
    assert store.get_due_outbox(now=time.time() + PERMISSION_NOTIFICATION_DELAY + 1) is not None

    session = _rows(store.path, "SELECT status FROM sessions")[0]
    assert session["status"] == "awaiting_approval"


def test_root_transcript_is_not_mistaken_for_subagent(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    root = _transcript(tmp_path, "root", subagent=False)
    assert store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "root-turn",
            transcript_path=str(root),
            prompt="总任务",
        )
    )
    assert store.ingest_hook(
        _event(
            "Stop",
            "root-turn",
            transcript_path=str(root),
            last_assistant_message="总结果",
        )
    )
    assert store.get_last_reply()["content"] == "总结果"  # type: ignore[index]


def test_invalid_transcript_metadata_fails_open_without_crashing(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text("not-json\n", encoding="utf-8")
    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))

    assert store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "root-missing",
            transcript_path=str(tmp_path / "missing.jsonl"),
            prompt="缺失 transcript 的主任务",
        )
    )
    assert store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "root-malformed",
            transcript_path=str(malformed),
            prompt="格式变化后的主任务",
        )
    )
    assert store.ingest_hook(
        _event(
            "UserPromptSubmit",
            "root-oversized",
            transcript_path=str(oversized),
            prompt="超大元数据后的主任务",
        )
    )


def test_next_root_turn_notifies_after_previous_root_completes(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(_event("UserPromptSubmit", "root-one", prompt="任务一"))
    store.ingest_hook(_event("Stop", "root-one", last_assistant_message="结果一"))

    assert store.ingest_hook(_event("UserPromptSubmit", "root-two", prompt="任务二"))
    assert store.ingest_hook(
        _event("Stop", "root-two", last_assistant_message="结果二")
    )
    assert [
        row["kind"] for row in _rows(store.path, "SELECT kind FROM outbox ORDER BY id")
    ] == ["task_started", "final_reply", "task_started", "final_reply"]
