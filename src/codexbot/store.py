from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import math
import ntpath
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import time
from collections.abc import Iterator
from typing import Any, Iterable

from .processes import HostProcess
from .security import hash_pairing_code, prompt_preview, redact_secrets


PERMISSION_NOTIFICATION_DELAY = 5.0
PERMISSION_NOTIFICATION_ENV = "CODEXBOT_NOTIFY_PERMISSION_REQUESTS"
DEFAULT_CONTENT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_LAST_REPLY_HISTORY_LIMIT = 20
LAST_REPLY_TTL_ENV = "CODEXBOT_LAST_REPLY_TTL_SECONDS"
OUTBOX_TTL_ENV = "CODEXBOT_OUTBOX_TTL_SECONDS"
LAST_REPLY_SCHEMA_VERSION = "2"
LAST_REPLY_SCHEMA_VERSION_KEY = "last_reply_schema_version"
SESSION_SCOPE_VERSION = "2"
SESSION_SCOPE_VERSION_KEY = "session_scope_version"
SESSION_KEY_PREFIX = "v2:"


@dataclass(frozen=True)
class OutboxItem:
    id: int
    event_key: str
    kind: str
    session_id: str
    turn_id: str | None
    payload: dict[str, Any]
    segments: list[str] | None
    segment_index: int
    attempts: int
    created_at: float


class Store:
    def __init__(
        self,
        path: Path,
        *,
        last_reply_ttl: float | None = None,
        outbox_ttl: float | None = None,
        last_reply_limit: int | None = None,
    ) -> None:
        self.path = Path(path)
        self.last_reply_ttl = self._resolve_ttl(
            last_reply_ttl,
            LAST_REPLY_TTL_ENV,
        )
        self.outbox_ttl = self._resolve_ttl(outbox_ttl, OUTBOX_TTL_ENV)
        self.last_reply_limit = self._resolve_history_limit(last_reply_limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _resolve_ttl(value: float | None, environment_key: str) -> float:
        configured = value
        if configured is None:
            raw = os.environ.get(environment_key)
            if raw is not None and raw.strip():
                try:
                    configured = float(raw)
                except ValueError:
                    configured = None
        if configured is None:
            return float(DEFAULT_CONTENT_TTL_SECONDS)
        try:
            seconds = float(configured)
        except (TypeError, ValueError):
            return float(DEFAULT_CONTENT_TTL_SECONDS)
        if not math.isfinite(seconds) or seconds < 0:
            return float(DEFAULT_CONTENT_TTL_SECONDS)
        return seconds

    @staticmethod
    def _resolve_history_limit(value: int | None) -> int:
        if value is None:
            return DEFAULT_LAST_REPLY_HISTORY_LIMIT
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return DEFAULT_LAST_REPLY_HISTORY_LIMIT
        return max(1, min(limit, 1000))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=3.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 3000")
            connection.execute("PRAGMA foreign_keys = ON")
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one short-lived connection and always close it after commit/rollback."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            if not journal_mode or str(journal_mode[0]).casefold() != "wal":
                connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    project TEXT NOT NULL,
                    model TEXT NOT NULL,
                    turn_id TEXT,
                    status TEXT NOT NULL,
                    prompt_preview TEXT,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hosts (
                    pid INTEGER NOT NULL,
                    create_time REAL NOT NULL,
                    kind TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (pid, create_time)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    payload_json TEXT NOT NULL,
                    segments_json TEXT,
                    segment_index INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS outbox_due
                    ON outbox (state, next_attempt_at, id);

                CREATE TABLE IF NOT EXISTS last_reply (
                    reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    project TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS inbound_messages (
                    message_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
                """
            )
            self._migrate_last_reply(connection)
            self._migrate_session_scope(connection)

    @staticmethod
    def _migrate_last_reply(connection: sqlite3.Connection) -> None:
        """Convert the old singleton reply row into the bounded history table."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(last_reply)").fetchall()
        }
        if "singleton" in columns:
            connection.execute(
                """
                CREATE TABLE last_reply_new (
                    reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                INSERT INTO last_reply_new(session_id, turn_id, project, model, content, created_at)
                SELECT session_id, turn_id, project, model, content, created_at
                FROM last_reply
                """
            )
            connection.execute("DROP TABLE last_reply")
            connection.execute("ALTER TABLE last_reply_new RENAME TO last_reply")

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS last_reply_recent
                ON last_reply (created_at DESC, reply_id DESC)
            """
        )

        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_REPLY_SCHEMA_VERSION_KEY, LAST_REPLY_SCHEMA_VERSION),
        )

    def _cleanup_expired_content(
        self,
        connection: sqlite3.Connection,
        *,
        now: float,
    ) -> None:
        """Remove full replies and queued payloads after their privacy TTL."""

        connection.execute(
            "DELETE FROM last_reply WHERE created_at < ?",
            (now - self.last_reply_ttl,),
        )
        connection.execute(
            "DELETE FROM outbox WHERE created_at < ?",
            (now - self.outbox_ttl,),
        )

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        value = str(cwd or "").strip()
        if not value:
            return ""
        return ntpath.normcase(ntpath.normpath(value))

    @classmethod
    def _scoped_session_id(
        cls,
        session_id: object,
        cwd: str,
        host: HostProcess | None = None,
    ) -> str:
        raw_session_id = str(session_id or "").strip() or "unknown"
        scope = cls._normalize_cwd(cwd)
        if not scope and raw_session_id == "unknown" and host is not None:
            scope = f"host:{host.pid}:{host.create_time:.6f}"
        identity = json.dumps(
            {"cwd": scope, "session": raw_session_id},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return SESSION_KEY_PREFIX + sha256(identity).hexdigest()

    @classmethod
    def _migrate_session_scope(cls, connection: sqlite3.Connection) -> None:
        version = connection.execute(
            "SELECT value FROM settings WHERE key = ?",
            (SESSION_SCOPE_VERSION_KEY,),
        ).fetchone()
        if version and str(version["value"]) == SESSION_SCOPE_VERSION:
            return

        rows = connection.execute("SELECT session_id, cwd FROM sessions").fetchall()
        mapping: dict[str, str] = {}
        for row in rows:
            old_session_id = str(row["session_id"])
            if old_session_id.startswith(SESSION_KEY_PREFIX):
                mapping[old_session_id] = old_session_id
            else:
                mapping[old_session_id] = cls._scoped_session_id(
                    old_session_id,
                    str(row["cwd"] or ""),
                )

        for old_session_id, new_session_id in mapping.items():
            if old_session_id == new_session_id:
                continue
            connection.execute(
                "UPDATE sessions SET session_id = ? WHERE session_id = ?",
                (new_session_id, old_session_id),
            )
            connection.execute(
                "UPDATE outbox SET session_id = ? WHERE session_id = ?",
                (new_session_id, old_session_id),
            )
            connection.execute(
                "UPDATE last_reply SET session_id = ? WHERE session_id = ?",
                (new_session_id, old_session_id),
            )

        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SESSION_SCOPE_VERSION_KEY, SESSION_SCOPE_VERSION),
        )

    @staticmethod
    def _project_name(cwd: str) -> str:
        if not cwd:
            return "unknown"
        if "\\" in cwd or (len(cwd) > 1 and cwd[1] == ":"):
            return PureWindowsPath(cwd).name or cwd
        return Path(cwd).name or cwd

    @staticmethod
    def _event_key(event: dict[str, Any], *, session_id: str | None = None) -> str:
        event_name = str(event.get("hook_event_name", ""))
        identity: dict[str, Any] = {
            "event": event_name,
            "session": event.get("session_id") if session_id is None else session_id,
            "turn": event.get("turn_id"),
        }
        if event_name == "PermissionRequest":
            identity["tool_use_id"] = event.get("tool_use_id")
            identity["tool"] = event.get("tool_name")
            identity["input"] = event.get("tool_input")
        elif event_name == "UserPromptSubmit":
            identity["prompt_hash"] = sha256(str(event.get("prompt", "")).encode("utf-8")).hexdigest()
        elif event_name == "Stop":
            identity["answer_hash"] = sha256(
                str(event.get("last_assistant_message", "")).encode("utf-8")
            ).hexdigest()
        else:
            identity["source"] = event.get("source") or event.get("reason")
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return sha256(encoded).hexdigest()

    @staticmethod
    def _permission_preview(event: dict[str, Any]) -> str:
        tool_input = event.get("tool_input")
        candidate = ""
        if isinstance(tool_input, dict):
            candidate = str(tool_input.get("description") or tool_input.get("command") or "")
            if not candidate:
                candidate = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
        elif tool_input is not None:
            candidate = str(tool_input)
        return prompt_preview(redact_secrets(candidate), 180) or "Codex 请求执行需要本机确认的操作"

    @staticmethod
    def _permission_notifications_enabled() -> bool:
        value = os.environ.get(PERMISSION_NOTIFICATION_ENV, "")
        return value.strip().casefold() in {"1", "true", "yes", "on"}

    @classmethod
    def _permission_mode_skips_notification(cls, event: dict[str, Any]) -> bool:
        # Codex auto-review still emits PermissionRequest, but the hook payload
        # does not identify the reviewer. Keep QQ quiet by default; opt in only
        # when a user explicitly wants manual approval reminders.
        if not cls._permission_notifications_enabled():
            return True
        mode = str(event.get("permission_mode") or "").casefold()
        return mode in {"dontask", "bypasspermissions"}

    @staticmethod
    def _resolve_permission_outbox(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_id: str | None,
        event: dict[str, Any],
    ) -> None:
        """Suppress a pending request once the corresponding tool actually runs."""

        event_tool = str(event.get("tool_name") or "")
        event_use_id = str(event.get("tool_use_id") or "")
        rows = connection.execute(
            """
            SELECT id, payload_json FROM outbox
            WHERE kind = 'permission_required'
              AND session_id = ?
              AND turn_id IS ?
              AND state = 'pending'
            ORDER BY id ASC
            """,
            (session_id, turn_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            request_tool = str(payload.get("tool") or "")
            request_use_id = str(payload.get("tool_use_id") or "")
            if request_use_id and event_use_id:
                matches = request_use_id == event_use_id
            else:
                matches = bool(request_tool and event_tool and request_tool == event_tool)
            if not matches:
                continue
            connection.execute(
                "UPDATE outbox SET state = 'suppressed', last_error = ? WHERE id = ?",
                ("permission request resolved by tool execution", int(row["id"])),
            )
            return

    def _trim_last_replies(self, connection: sqlite3.Connection, session_id: str) -> None:
        rows = connection.execute(
            "SELECT reply_id FROM last_reply "
            "WHERE session_id = ? ORDER BY created_at DESC, reply_id DESC LIMIT ?",
            (session_id, self.last_reply_limit),
        ).fetchall()
        keep_ids = [int(row["reply_id"]) for row in rows]
        if not keep_ids:
            return
        placeholders = ",".join("?" for _ in keep_ids)
        connection.execute(
            f"DELETE FROM last_reply WHERE session_id = ? AND reply_id NOT IN ({placeholders})",
            (session_id, *keep_ids),
        )

    def ingest_hook(self, event: dict[str, Any], host: HostProcess | None = None) -> bool:
        event_name = str(event.get("hook_event_name", ""))
        if event_name not in {
            "SessionStart",
            "UserPromptSubmit",
            "PermissionRequest",
            "PostToolUse",
            "Stop",
            "SessionEnd",
        }:
            raise ValueError(f"unsupported hook event: {event_name}")

        now = time.time()
        raw_session_id = str(event.get("session_id") or "unknown")
        turn_id = str(event["turn_id"]) if event.get("turn_id") is not None else None
        cwd = str(event.get("cwd") or "")
        project = self._project_name(cwd)
        session_id = self._scoped_session_id(raw_session_id, cwd, host)
        model = str(event.get("model") or "unknown")
        status = "idle"
        preview: str | None = None
        payload: dict[str, Any] | None = None
        kind: str | None = None

        if event_name == "UserPromptSubmit":
            status = "running"
            preview = prompt_preview(str(event.get("prompt") or ""), 120)
            kind = "task_started"
            payload = {"project": project, "model": model, "preview": preview, "created_at": now}
        elif event_name == "PermissionRequest":
            status = "running"
            if not self._permission_mode_skips_notification(event):
                status = "awaiting_approval"
                kind = "permission_required"
                payload = {
                    "project": project,
                    "model": model,
                    "tool": str(event.get("tool_name") or "unknown"),
                    "tool_use_id": str(event.get("tool_use_id") or ""),
                    "reason": self._permission_preview(event),
                    "created_at": now,
                }
        elif event_name == "PostToolUse":
            status = "running"
        elif event_name == "Stop":
            status = "completed"
            # The bound user explicitly requests the complete final answer via
            # proactive delivery and /last. Redacting token-like source-code
            # variables here would silently corrupt that answer; privacy is
            # instead bounded by the reply TTL and single-user QQ binding.
            answer = str(event.get("last_assistant_message") or "")
            if answer:
                kind = "final_reply"
                payload = {
                    "project": project,
                    "model": model,
                    "content": answer,
                    "created_at": now,
                }
        elif event_name == "SessionEnd":
            status = "closed"
        elif event_name == "SessionStart" and str(event.get("source") or "") == "compact":
            status = "preserve"

        event_key = self._event_key(event, session_id=session_id)
        legacy_event_key = self._event_key(event)
        inserted = False
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            if host is not None:
                connection.execute(
                    """
                    INSERT INTO hosts(pid, create_time, kind, last_seen)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(pid, create_time) DO UPDATE SET
                        kind = excluded.kind,
                        last_seen = excluded.last_seen
                    """,
                    (host.pid, host.create_time, host.kind, now),
                )

            if event_name == "PostToolUse":
                self._resolve_permission_outbox(
                    connection,
                    session_id=session_id,
                    turn_id=turn_id,
                    event=event,
                )

            existing = connection.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            effective_status = existing["status"] if status == "preserve" and existing else (
                "running" if status == "preserve" else status
            )
            connection.execute(
                """
                INSERT INTO sessions(session_id, cwd, project, model, turn_id, status, prompt_preview, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    cwd = excluded.cwd,
                    project = excluded.project,
                    model = excluded.model,
                    turn_id = COALESCE(excluded.turn_id, sessions.turn_id),
                    status = excluded.status,
                    prompt_preview = COALESCE(excluded.prompt_preview, sessions.prompt_preview),
                    updated_at = excluded.updated_at
                """,
                (session_id, cwd, project, model, turn_id, effective_status, preview, now),
            )

            existing_event = None
            if kind and payload is not None:
                existing_event = connection.execute(
                    "SELECT 1 FROM outbox "
                    "WHERE event_key = ? OR (event_key = ? AND session_id = ?) LIMIT 1",
                    (event_key, legacy_event_key, session_id),
                ).fetchone()

            if event_name == "Stop" and payload is not None and existing_event is None:
                connection.execute(
                    """
                    INSERT INTO last_reply(session_id, turn_id, project, model, content, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, turn_id, project, model, payload["content"], now),
                )
                self._trim_last_replies(connection, session_id)

            if kind and payload is not None:
                muted = connection.execute(
                    "SELECT value FROM settings WHERE key = 'muted'"
                ).fetchone()
                initial_state = "suppressed" if muted and muted["value"] == "1" else "pending"
                if existing_event is None:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO outbox(
                            event_key, kind, session_id, turn_id, payload_json, state, created_at,
                            next_attempt_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_key,
                            kind,
                            session_id,
                            turn_id,
                            json.dumps(payload, ensure_ascii=False),
                            initial_state,
                            now,
                            now + PERMISSION_NOTIFICATION_DELAY
                            if kind == "permission_required" and initial_state == "pending"
                            else 0,
                            "notifications muted" if initial_state == "suppressed" else None,
                        ),
                    )
                    inserted = cursor.rowcount == 1
        return inserted

    def get_setting(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_settings(self, keys: Iterable[str]) -> None:
        values = tuple(keys)
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        with self._connection() as connection:
            connection.execute(f"DELETE FROM settings WHERE key IN ({placeholders})", values)

    def set_daemon_info(self, pid: int, create_time: float) -> None:
        self.set_setting("daemon", json.dumps({"pid": pid, "create_time": create_time}))

    def get_daemon_info(self) -> tuple[int, float] | None:
        raw = self.get_setting("daemon")
        if not raw:
            return None
        try:
            value = json.loads(raw)
            return int(value["pid"]), float(value["create_time"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def clear_daemon_info(self, pid: int) -> None:
        current = self.get_daemon_info()
        if current and current[0] == pid:
            self.delete_settings(["daemon"])

    def list_hosts(self) -> list[HostProcess]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT pid, create_time, kind FROM hosts ORDER BY last_seen DESC"
            ).fetchall()
        return [HostProcess(int(row["pid"]), float(row["create_time"]), str(row["kind"])) for row in rows]

    def remove_hosts(self, hosts: Iterable[HostProcess]) -> None:
        values = [(host.pid, host.create_time) for host in hosts]
        if not values:
            return
        with self._connection() as connection:
            connection.executemany("DELETE FROM hosts WHERE pid = ? AND create_time = ?", values)

    def get_bound_openid(self) -> str | None:
        return self.get_setting("bound_openid")

    def create_pairing(self, code: str, expires_at: float) -> None:
        self.set_setting("pairing_hash", hash_pairing_code(code))
        self.set_setting("pairing_expires_at", str(expires_at))

    def consume_pairing(self, code: str, openid: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        supplied = hash_pairing_code(code)
        with self._connection() as connection:
            hash_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'pairing_hash'"
            ).fetchone()
            expiry_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'pairing_expires_at'"
            ).fetchone()
            if not hash_row or not expiry_row:
                return False
            try:
                valid_time = now <= float(expiry_row["value"])
            except ValueError:
                return False
            if not valid_time or not hmac.compare_digest(str(hash_row["value"]), supplied):
                return False

            connection.execute(
                "INSERT INTO settings(key, value) VALUES ('bound_openid', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (openid,),
            )
            connection.execute("DELETE FROM settings WHERE key IN ('pairing_hash', 'pairing_expires_at')")
            connection.execute(
                "UPDATE outbox SET state = 'suppressed', last_error = 'created before QQ binding' "
                "WHERE state = 'pending' AND created_at <= ?",
                (now,),
            )
        return True

    def pairing_status(self, *, now: float | None = None) -> tuple[bool, float | None]:
        now = time.time() if now is None else now
        raw_hash = self.get_setting("pairing_hash")
        raw_expiry = self.get_setting("pairing_expires_at")
        if not raw_hash or not raw_expiry:
            return False, None
        try:
            expiry = float(raw_expiry)
        except ValueError:
            return False, None
        return expiry >= now, expiry

    def is_muted(self) -> bool:
        return self.get_setting("muted") == "1"

    def set_muted(self, muted: bool) -> None:
        self.set_setting("muted", "1" if muted else "0")

    def remember_inbound(self, message_id: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound_messages(message_id, created_at) VALUES (?, ?)",
                (message_id, now),
            )
        return cursor.rowcount == 1

    def get_due_outbox(self, *, now: float | None = None) -> OutboxItem | None:
        now = time.time() if now is None else now
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            row = connection.execute(
                """
                SELECT * FROM outbox
                WHERE state = 'pending' AND next_attempt_at <= ?
                ORDER BY id ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
        if not row:
            return None
        return OutboxItem(
            id=int(row["id"]),
            event_key=str(row["event_key"]),
            kind=str(row["kind"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]) if row["turn_id"] is not None else None,
            payload=json.loads(str(row["payload_json"])),
            segments=json.loads(str(row["segments_json"])) if row["segments_json"] else None,
            segment_index=int(row["segment_index"]),
            attempts=int(row["attempts"]),
            created_at=float(row["created_at"]),
        )

    def prepare_segments(self, item_id: int, segments: list[str]) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET segments_json = ?, segment_index = 0 WHERE id = ?",
                (json.dumps(segments, ensure_ascii=False), item_id),
            )

    def replace_segments(self, item_id: int, segments: list[str], index: int) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET segments_json = ?, segment_index = ? WHERE id = ?",
                (json.dumps(segments, ensure_ascii=False), index, item_id),
            )

    def advance_segment(self, item_id: int, current_index: int, total: int) -> None:
        with self._connection() as connection:
            if current_index + 1 >= total:
                connection.execute(
                    "UPDATE outbox SET state = 'delivered', segment_index = ?, last_error = NULL WHERE id = ?",
                    (total, item_id),
                )
            else:
                connection.execute(
                    "UPDATE outbox SET segment_index = ?, attempts = 0, next_attempt_at = 0, last_error = NULL "
                    "WHERE id = ?",
                    (current_index + 1, item_id),
                )

    def reschedule(self, item_id: int, *, delay: float, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE outbox
                SET attempts = attempts + 1,
                    next_attempt_at = ?,
                    last_error = ?,
                    state = 'pending'
                WHERE id = ?
                """,
                (time.time() + delay, error[:500], item_id),
            )

    def mark_outbox(self, item_id: int, state: str, reason: str) -> None:
        if state not in {"delivered", "suppressed", "failed_permanent"}:
            raise ValueError(state)
        with self._connection() as connection:
            connection.execute(
                "UPDATE outbox SET state = ?, last_error = ? WHERE id = ?",
                (state, reason[:500], item_id),
            )

    def get_sessions_for_status(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            active = connection.execute(
                """
                SELECT * FROM sessions
                WHERE status IN ('running', 'awaiting_approval', 'idle')
                ORDER BY updated_at DESC LIMIT 3
                """
            ).fetchall()
            rows = active or connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_last_reply(
        self,
        *,
        project: str | None = None,
        session_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        now = time.time() if now is None else now
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            conditions: list[str] = []
            parameters: list[object] = []
            if project is not None:
                conditions.append("project = ? COLLATE NOCASE")
                parameters.append(project)
            if session_id is not None:
                conditions.append("session_id = ?")
                parameters.append(session_id)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            row = connection.execute(
                f"SELECT * FROM last_reply {where} "
                "ORDER BY created_at DESC, reply_id DESC LIMIT 1",
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def get_last_replies(
        self,
        *,
        project: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        requested_limit = self.last_reply_limit if limit is None else max(1, min(int(limit), 1000))
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            conditions: list[str] = []
            parameters: list[object] = []
            if project is not None:
                conditions.append("project = ? COLLATE NOCASE")
                parameters.append(project)
            if session_id is not None:
                conditions.append("session_id = ?")
                parameters.append(session_id)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = connection.execute(
                f"SELECT * FROM last_reply {where} "
                "ORDER BY created_at DESC, reply_id DESC LIMIT ?",
                (*parameters, requested_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_last_reply_projects(self, *, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            rows = connection.execute(
                "SELECT project, MAX(created_at) AS latest FROM last_reply "
                "GROUP BY project ORDER BY latest DESC, project COLLATE NOCASE"
            ).fetchall()
        return [str(row["project"]) for row in rows]

    def cleanup(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - 7 * 24 * 60 * 60
        with self._connection() as connection:
            self._cleanup_expired_content(connection, now=now)
            connection.execute("DELETE FROM inbound_messages WHERE created_at < ?", (cutoff,))
            if not self._permission_notifications_enabled():
                connection.execute(
                    "UPDATE outbox SET state = 'suppressed', last_error = ? "
                    "WHERE state = 'pending' AND kind = 'permission_required'",
                    ("permission notifications disabled",),
                )
