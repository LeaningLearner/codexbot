//! Short-lived SQLite state store for hook ingestion and reliable delivery.

use crate::processes::{DaemonState, HostProcess};
use crate::security::{hash_pairing_code, prompt_preview, redact_secrets};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

pub const PERMISSION_NOTIFICATION_DELAY: f64 = 5.0;
pub const PERMISSION_NOTIFICATION_ENV: &str = "CODEXBOT_NOTIFY_PERMISSION_REQUESTS";
pub const DEFAULT_CONTENT_TTL_SECONDS: f64 = 7.0 * 24.0 * 60.0 * 60.0;
pub const DEFAULT_LAST_REPLY_HISTORY_LIMIT: usize = 20;
pub const LAST_REPLY_TTL_ENV: &str = "CODEXBOT_LAST_REPLY_TTL_SECONDS";
pub const OUTBOX_TTL_ENV: &str = "CODEXBOT_OUTBOX_TTL_SECONDS";
pub const LAST_REPLY_SCHEMA_VERSION: &str = "2";
pub const LAST_REPLY_SCHEMA_VERSION_KEY: &str = "last_reply_schema_version";
pub const SESSION_SCOPE_VERSION: &str = "2";
pub const SESSION_SCOPE_VERSION_KEY: &str = "session_scope_version";
pub const SESSION_KEY_PREFIX: &str = "v2:";
pub const TRANSCRIPT_METADATA_MAX_BYTES: usize = 64 * 1024;

const SUBAGENT_LIFECYCLE_EVENTS: &[&str] = &["SubagentStart", "SubagentStop"];
const SUBAGENT_IDENTITY_FIELDS: &[&str] = &["agent_id", "agent_type", "agent_transcript_path"];

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("SQLite state error: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("state I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid JSON state: {0}")]
    Json(#[from] serde_json::Error),
    #[error("hook payload must be a JSON object")]
    PayloadNotObject,
    #[error("unsupported hook event: {0}")]
    UnsupportedHookEvent(String),
    #[error("invalid outbox state: {0}")]
    InvalidOutboxState(String),
}

pub type StoreResult<T> = Result<T, StoreError>;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OutboxItem {
    pub id: i64,
    pub event_key: String,
    pub kind: String,
    pub session_id: String,
    pub turn_id: Option<String>,
    pub payload: Value,
    pub segments: Option<Vec<String>>,
    pub segment_index: usize,
    pub attempts: u32,
    pub created_at: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SessionInfo {
    pub session_id: String,
    pub cwd: String,
    pub project: String,
    pub model: String,
    pub turn_id: Option<String>,
    pub status: String,
    pub prompt_preview: Option<String>,
    pub updated_at: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LastReply {
    pub reply_id: i64,
    pub session_id: String,
    pub turn_id: Option<String>,
    pub project: String,
    pub model: String,
    pub content: String,
    pub created_at: f64,
}

#[derive(Debug, Clone)]
pub struct Store {
    pub path: PathBuf,
    pub last_reply_ttl: f64,
    pub outbox_ttl: f64,
    pub last_reply_limit: usize,
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn sha256_hex(value: &[u8]) -> String {
    hex::encode(Sha256::digest(value))
}

fn py_string(value: Option<&Value>, default: &str) -> String {
    match value {
        None | Some(Value::Null) => default.to_owned(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Bool(value)) => if *value { "True" } else { "False" }.to_owned(),
        Some(Value::Number(value)) => value.to_string(),
        Some(value) => value.to_string(),
    }
}

fn nonempty_string(value: Option<&Value>) -> Option<String> {
    let value = py_string(value, "");
    (!value.is_empty()).then_some(value)
}

fn python_json_sorted(value: &Value) -> String {
    match value {
        Value::Null => "null".to_owned(),
        Value::Bool(value) => value.to_string(),
        Value::Number(value) => value.to_string(),
        Value::String(value) => {
            serde_json::to_string(value).expect("serializing a string cannot fail")
        }
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_json_sorted)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => {
            let sorted: BTreeMap<&str, &Value> = values
                .iter()
                .map(|(key, value)| (key.as_str(), value))
                .collect();
            let body = sorted
                .into_iter()
                .map(|(key, value)| {
                    format!(
                        "{}: {}",
                        serde_json::to_string(key).expect("serializing a key cannot fail"),
                        python_json_sorted(value)
                    )
                })
                .collect::<Vec<_>>()
                .join(", ");
            format!("{{{body}}}")
        }
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    let maximum = left.len().max(right.len());
    for index in 0..maximum {
        let lhs = left.get(index).copied().unwrap_or(0);
        let rhs = right.get(index).copied().unwrap_or(0);
        difference |= usize::from(lhs ^ rhs);
    }
    difference == 0
}

impl Store {
    pub fn new(path: impl Into<PathBuf>) -> StoreResult<Self> {
        Self::with_options(path, None, None, None)
    }

    pub fn with_options(
        path: impl Into<PathBuf>,
        last_reply_ttl: Option<f64>,
        outbox_ttl: Option<f64>,
        last_reply_limit: Option<usize>,
    ) -> StoreResult<Self> {
        let path = path.into();
        if let Some(parent) = path.parent().filter(|value| !value.as_os_str().is_empty()) {
            std::fs::create_dir_all(parent)?;
        }
        let store = Self {
            path,
            last_reply_ttl: Self::resolve_ttl(last_reply_ttl, LAST_REPLY_TTL_ENV),
            outbox_ttl: Self::resolve_ttl(outbox_ttl, OUTBOX_TTL_ENV),
            last_reply_limit: last_reply_limit
                .unwrap_or(DEFAULT_LAST_REPLY_HISTORY_LIMIT)
                .clamp(1, 1_000),
        };
        store.initialize()?;
        Ok(store)
    }

    fn resolve_ttl(value: Option<f64>, environment_key: &str) -> f64 {
        let configured = value.or_else(|| {
            env::var(environment_key)
                .ok()
                .filter(|raw| !raw.trim().is_empty())
                .and_then(|raw| raw.parse().ok())
        });
        match configured {
            Some(seconds) if seconds.is_finite() && seconds >= 0.0 => seconds,
            _ => DEFAULT_CONTENT_TTL_SECONDS,
        }
    }

    fn connect(&self) -> StoreResult<Connection> {
        let connection = Connection::open(&self.path)?;
        connection.busy_timeout(std::time::Duration::from_secs(3))?;
        connection.pragma_update(None, "foreign_keys", true)?;
        Ok(connection)
    }

    fn initialize(&self) -> StoreResult<()> {
        let connection = self.connect()?;
        let journal_mode: String =
            connection.pragma_query_value(None, "journal_mode", |row| row.get(0))?;
        if !journal_mode.eq_ignore_ascii_case("wal") {
            connection.pragma_update(None, "journal_mode", "WAL")?;
        }
        connection.execute_batch(
            r#"
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
            "#,
        )?;
        Self::migrate_last_reply(&connection)?;
        Self::migrate_session_scope(&connection)?;
        Ok(())
    }

    fn migrate_last_reply(connection: &Connection) -> StoreResult<()> {
        let columns = {
            let mut statement = connection.prepare("PRAGMA table_info(last_reply)")?;
            statement
                .query_map([], |row| row.get::<_, String>(1))?
                .collect::<Result<Vec<_>, _>>()?
        };
        if columns.iter().any(|column| column == "singleton") {
            connection.execute_batch(
                r#"
                CREATE TABLE last_reply_new (
                    reply_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT,
                    project TEXT NOT NULL,
                    model TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                INSERT INTO last_reply_new(session_id, turn_id, project, model, content, created_at)
                SELECT session_id, turn_id, project, model, content, created_at FROM last_reply;
                DROP TABLE last_reply;
                ALTER TABLE last_reply_new RENAME TO last_reply;
                "#,
            )?;
        }
        connection.execute_batch(
            "CREATE INDEX IF NOT EXISTS last_reply_recent \
             ON last_reply (created_at DESC, reply_id DESC);",
        )?;
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?1, ?2) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![LAST_REPLY_SCHEMA_VERSION_KEY, LAST_REPLY_SCHEMA_VERSION],
        )?;
        Ok(())
    }

    fn migrate_session_scope(connection: &Connection) -> StoreResult<()> {
        let version: Option<String> = connection
            .query_row(
                "SELECT value FROM settings WHERE key = ?1",
                [SESSION_SCOPE_VERSION_KEY],
                |row| row.get(0),
            )
            .optional()?;
        if version.as_deref() == Some(SESSION_SCOPE_VERSION) {
            return Ok(());
        }

        let rows = {
            let mut statement = connection.prepare("SELECT session_id, cwd FROM sessions")?;
            statement
                .query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        for (old_session_id, cwd) in rows {
            if old_session_id.starts_with(SESSION_KEY_PREFIX) {
                continue;
            }
            let new_session_id = Self::scoped_session_id(&old_session_id, &cwd, None);
            connection.execute(
                "UPDATE sessions SET session_id = ?1 WHERE session_id = ?2",
                params![new_session_id, old_session_id],
            )?;
            connection.execute(
                "UPDATE outbox SET session_id = ?1 WHERE session_id = ?2",
                params![new_session_id, old_session_id],
            )?;
            connection.execute(
                "UPDATE last_reply SET session_id = ?1 WHERE session_id = ?2",
                params![new_session_id, old_session_id],
            )?;
        }
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?1, ?2) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![SESSION_SCOPE_VERSION_KEY, SESSION_SCOPE_VERSION],
        )?;
        Ok(())
    }

    fn cleanup_expired_content(&self, connection: &Connection, now: f64) -> StoreResult<()> {
        connection.execute(
            "DELETE FROM last_reply WHERE created_at < ?1",
            [now - self.last_reply_ttl],
        )?;
        connection.execute(
            "DELETE FROM outbox WHERE created_at < ?1",
            [now - self.outbox_ttl],
        )?;
        Ok(())
    }

    pub fn normalize_cwd(cwd: &str) -> String {
        let value = cwd.trim();
        if value.is_empty() {
            return String::new();
        }
        let replaced = value.replace('/', "\\").to_lowercase();
        let drive = replaced
            .as_bytes()
            .get(1)
            .is_some_and(|value| *value == b':')
            .then(|| replaced[..2].to_owned());
        let unc = replaced.starts_with("\\\\");
        let absolute = drive.is_some() && replaced[2..].starts_with('\\');
        let rooted = replaced.starts_with('\\');
        let body = if drive.is_some() {
            &replaced[2..]
        } else {
            &replaced
        };
        let mut parts: Vec<&str> = Vec::new();
        for part in body.split('\\') {
            match part {
                "" | "." => {}
                ".." if parts.last().is_some_and(|last| *last != "..") => {
                    parts.pop();
                }
                ".." if !absolute && !unc => parts.push(part),
                ".." => {}
                _ => parts.push(part),
            }
        }
        let joined = parts.join("\\");
        if let Some(drive) = drive {
            if absolute {
                if joined.is_empty() {
                    format!("{drive}\\")
                } else {
                    format!("{drive}\\{joined}")
                }
            } else {
                format!("{drive}{joined}")
            }
        } else if unc {
            format!("\\\\{joined}")
        } else if rooted {
            if joined.is_empty() {
                "\\".to_owned()
            } else {
                format!("\\{joined}")
            }
        } else if joined.is_empty() {
            ".".to_owned()
        } else {
            joined
        }
    }

    pub fn scoped_session_id(session_id: &str, cwd: &str, host: Option<&HostProcess>) -> String {
        let raw_session_id = if session_id.trim().is_empty() {
            "unknown"
        } else {
            session_id.trim()
        };
        let mut scope = Self::normalize_cwd(cwd);
        if scope.is_empty() && raw_session_id == "unknown" {
            if let Some(host) = host {
                scope = format!("host:{}:{:.6}", host.pid, host.create_time);
            }
        }
        let identity = format!(
            "{{\"cwd\": {}, \"session\": {}}}",
            serde_json::to_string(&scope).expect("serializing cwd cannot fail"),
            serde_json::to_string(raw_session_id).expect("serializing a session ID cannot fail")
        );
        format!("{SESSION_KEY_PREFIX}{}", sha256_hex(identity.as_bytes()))
    }

    pub fn project_name(cwd: &str) -> String {
        if cwd.is_empty() {
            return "unknown".to_owned();
        }
        if cwd.contains('\\') || cwd.as_bytes().get(1) == Some(&b':') {
            let stripped = cwd.trim_end_matches(['\\', '/']);
            return stripped
                .rsplit(['\\', '/'])
                .next()
                .filter(|value| !value.is_empty())
                .unwrap_or(cwd)
                .to_owned();
        }
        Path::new(cwd)
            .file_name()
            .map(|value| value.to_string_lossy().into_owned())
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| cwd.to_owned())
    }

    pub fn event_key(event: &Value, session_id: Option<&str>) -> StoreResult<String> {
        let object = event.as_object().ok_or(StoreError::PayloadNotObject)?;
        let event_name = py_string(object.get("hook_event_name"), "");
        let mut identity = Map::new();
        identity.insert("event".to_owned(), Value::String(event_name.clone()));
        identity.insert(
            "session".to_owned(),
            session_id
                .map(|value| Value::String(value.to_owned()))
                .unwrap_or_else(|| object.get("session_id").cloned().unwrap_or(Value::Null)),
        );
        identity.insert(
            "turn".to_owned(),
            object.get("turn_id").cloned().unwrap_or(Value::Null),
        );
        match event_name.as_str() {
            "PermissionRequest" => {
                identity.insert(
                    "tool_use_id".to_owned(),
                    object.get("tool_use_id").cloned().unwrap_or(Value::Null),
                );
                identity.insert(
                    "tool".to_owned(),
                    object.get("tool_name").cloned().unwrap_or(Value::Null),
                );
                identity.insert(
                    "input".to_owned(),
                    object.get("tool_input").cloned().unwrap_or(Value::Null),
                );
            }
            "UserPromptSubmit" => {
                let prompt = py_string(object.get("prompt"), "");
                identity.insert(
                    "prompt_hash".to_owned(),
                    Value::String(sha256_hex(prompt.as_bytes())),
                );
            }
            "Stop" => {
                let answer = py_string(object.get("last_assistant_message"), "");
                identity.insert(
                    "answer_hash".to_owned(),
                    Value::String(sha256_hex(answer.as_bytes())),
                );
            }
            _ => {
                let source = object
                    .get("source")
                    .filter(|value| !value.is_null() && **value != Value::String(String::new()))
                    .or_else(|| object.get("reason"))
                    .cloned()
                    .unwrap_or(Value::Null);
                identity.insert("source".to_owned(), source);
            }
        }
        Ok(sha256_hex(
            python_json_sorted(&Value::Object(identity)).as_bytes(),
        ))
    }

    fn permission_preview(event: &Map<String, Value>) -> String {
        let candidate = match event.get("tool_input") {
            Some(Value::Object(input)) => nonempty_string(input.get("description"))
                .or_else(|| nonempty_string(input.get("command")))
                .unwrap_or_else(|| python_json_sorted(&Value::Object(input.clone()))),
            Some(value) if !value.is_null() => py_string(Some(value), ""),
            _ => String::new(),
        };
        let preview = prompt_preview(&redact_secrets(&candidate), 180);
        if preview.is_empty() {
            "Codex 请求执行需要本机确认的操作".to_owned()
        } else {
            preview
        }
    }

    pub fn permission_notifications_enabled() -> bool {
        let value = env::var(PERMISSION_NOTIFICATION_ENV)
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        matches!(value.as_str(), "1" | "true" | "yes" | "on")
    }

    fn permission_mode_skips_notification(event: &Map<String, Value>) -> bool {
        if !Self::permission_notifications_enabled() {
            return true;
        }
        matches!(
            py_string(event.get("permission_mode"), "")
                .to_ascii_lowercase()
                .as_str(),
            "dontask" | "bypasspermissions"
        )
    }

    fn resolve_permission_outbox(
        connection: &Connection,
        session_id: &str,
        turn_id: Option<&str>,
        event: &Map<String, Value>,
    ) -> StoreResult<()> {
        let event_tool = py_string(event.get("tool_name"), "");
        let event_use_id = py_string(event.get("tool_use_id"), "");
        let rows = {
            let mut statement = connection.prepare(
                "SELECT id, payload_json FROM outbox \
                 WHERE kind = 'permission_required' AND session_id = ?1 \
                 AND turn_id IS ?2 AND state = 'pending' ORDER BY id ASC",
            )?;
            statement
                .query_map(params![session_id, turn_id], |row| {
                    Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
                })?
                .collect::<Result<Vec<_>, _>>()?
        };
        for (id, payload_json) in rows {
            let Ok(payload) = serde_json::from_str::<Value>(&payload_json) else {
                continue;
            };
            let request_tool = py_string(payload.get("tool"), "");
            let request_use_id = py_string(payload.get("tool_use_id"), "");
            let matches = if !request_use_id.is_empty() && !event_use_id.is_empty() {
                request_use_id == event_use_id
            } else {
                !request_tool.is_empty() && !event_tool.is_empty() && request_tool == event_tool
            };
            if matches {
                connection.execute(
                    "UPDATE outbox SET state = 'suppressed', last_error = ?1 WHERE id = ?2",
                    params!["permission request resolved by tool execution", id],
                )?;
                break;
            }
        }
        Ok(())
    }

    fn trim_last_replies(&self, connection: &Connection, session_id: &str) -> StoreResult<()> {
        connection.execute(
            "DELETE FROM last_reply WHERE session_id = ?1 AND reply_id NOT IN (\
                 SELECT reply_id FROM last_reply WHERE session_id = ?1 \
                 ORDER BY created_at DESC, reply_id DESC LIMIT ?2\
             )",
            params![session_id, self.last_reply_limit as i64],
        )?;
        Ok(())
    }

    fn has_subagent_identity(event: &Map<String, Value>) -> bool {
        SUBAGENT_IDENTITY_FIELDS
            .iter()
            .any(|field| !py_string(event.get(*field), "").trim().is_empty())
    }

    fn transcript_marks_subagent(event: &Map<String, Value>) -> bool {
        let Some(path) = event
            .get("transcript_path")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
        else {
            return false;
        };
        let Ok(file) = File::open(path) else {
            return false;
        };
        let mut line = Vec::new();
        let Ok(_) = BufReader::new(file)
            .take((TRANSCRIPT_METADATA_MAX_BYTES + 1) as u64)
            .read_until(b'\n', &mut line)
        else {
            return false;
        };
        if line.is_empty() || line.len() > TRANSCRIPT_METADATA_MAX_BYTES {
            return false;
        }
        if line.starts_with(&[0xef, 0xbb, 0xbf]) {
            line.drain(..3);
        }
        let Ok(record) = serde_json::from_slice::<Value>(&line) else {
            return false;
        };
        if record.get("type").and_then(Value::as_str) != Some("session_meta") {
            return false;
        }
        let Some(metadata) = record.get("payload").and_then(Value::as_object) else {
            return false;
        };
        let source_is_subagent = metadata
            .get("source")
            .and_then(Value::as_object)
            .is_some_and(|source| source.contains_key("subagent"));
        metadata
            .get("thread_source")
            .and_then(Value::as_str)
            .is_some_and(|source| source.eq_ignore_ascii_case("subagent"))
            || source_is_subagent
    }

    pub fn ingest_hook(&self, event: &Value, host: Option<&HostProcess>) -> StoreResult<bool> {
        self.ingest_hook_at(event, host, now_seconds())
    }

    pub fn ingest_hook_at(
        &self,
        event: &Value,
        host: Option<&HostProcess>,
        now: f64,
    ) -> StoreResult<bool> {
        let object = event.as_object().ok_or(StoreError::PayloadNotObject)?;
        let event_name = py_string(object.get("hook_event_name"), "");
        if SUBAGENT_LIFECYCLE_EVENTS.contains(&event_name.as_str()) {
            return Ok(false);
        }
        if matches!(event_name.as_str(), "UserPromptSubmit" | "Stop")
            && (Self::has_subagent_identity(object) || Self::transcript_marks_subagent(object))
        {
            return Ok(false);
        }
        if !matches!(
            event_name.as_str(),
            "SessionStart"
                | "UserPromptSubmit"
                | "PermissionRequest"
                | "PostToolUse"
                | "Stop"
                | "SessionEnd"
        ) {
            return Err(StoreError::UnsupportedHookEvent(event_name));
        }

        let raw_session_id = py_string(object.get("session_id"), "unknown");
        let turn_id = object
            .get("turn_id")
            .filter(|value| !value.is_null())
            .map(|value| py_string(Some(value), ""));
        let cwd = py_string(object.get("cwd"), "");
        let project = Self::project_name(&cwd);
        let session_id = Self::scoped_session_id(&raw_session_id, &cwd, host);
        let model = py_string(object.get("model"), "unknown");
        let mut status = "idle".to_owned();
        let mut preview: Option<String> = None;
        let mut payload: Option<Value> = None;
        let mut kind: Option<&str> = None;

        match event_name.as_str() {
            "UserPromptSubmit" => {
                status = "running".to_owned();
                preview = Some(prompt_preview(&py_string(object.get("prompt"), ""), 120));
                kind = Some("task_started");
                payload = Some(serde_json::json!({
                    "project": project,
                    "model": model,
                    "preview": preview,
                    "created_at": now,
                }));
            }
            "PermissionRequest" => {
                status = "running".to_owned();
                if !Self::permission_mode_skips_notification(object) {
                    status = "awaiting_approval".to_owned();
                    kind = Some("permission_required");
                    payload = Some(serde_json::json!({
                        "project": project,
                        "model": model,
                        "tool": py_string(object.get("tool_name"), "unknown"),
                        "tool_use_id": py_string(object.get("tool_use_id"), ""),
                        "reason": Self::permission_preview(object),
                        "created_at": now,
                    }));
                }
            }
            "PostToolUse" => status = "running".to_owned(),
            "Stop" => {
                status = "completed".to_owned();
                let answer = py_string(object.get("last_assistant_message"), "");
                if !answer.is_empty() {
                    kind = Some("final_reply");
                    payload = Some(serde_json::json!({
                        "project": project,
                        "model": model,
                        "content": answer,
                        "created_at": now,
                    }));
                }
            }
            "SessionEnd" => status = "closed".to_owned(),
            "SessionStart"
                if py_string(object.get("source"), "").eq_ignore_ascii_case("compact") =>
            {
                status = "preserve".to_owned();
            }
            _ => {}
        }

        let event_key = Self::event_key(event, Some(&session_id))?;
        let legacy_event_key = Self::event_key(event, None)?;
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        self.cleanup_expired_content(&transaction, now)?;
        if let Some(host) = host {
            transaction.execute(
                "INSERT INTO hosts(pid, create_time, kind, last_seen) VALUES (?1, ?2, ?3, ?4) \
                 ON CONFLICT(pid, create_time) DO UPDATE SET \
                 kind = excluded.kind, last_seen = excluded.last_seen",
                params![host.pid, host.create_time, &host.kind, now],
            )?;
        }
        if event_name == "PostToolUse" {
            Self::resolve_permission_outbox(&transaction, &session_id, turn_id.as_deref(), object)?;
        }

        let existing_status: Option<String> = transaction
            .query_row(
                "SELECT status FROM sessions WHERE session_id = ?1",
                [&session_id],
                |row| row.get(0),
            )
            .optional()?;
        let effective_status = if status == "preserve" {
            existing_status.unwrap_or_else(|| "running".to_owned())
        } else {
            status
        };
        transaction.execute(
            "INSERT INTO sessions(\
                 session_id, cwd, project, model, turn_id, status, prompt_preview, updated_at\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8) \
             ON CONFLICT(session_id) DO UPDATE SET \
                 cwd = excluded.cwd, project = excluded.project, model = excluded.model, \
                 turn_id = COALESCE(excluded.turn_id, sessions.turn_id), \
                 status = excluded.status, \
                 prompt_preview = COALESCE(excluded.prompt_preview, sessions.prompt_preview), \
                 updated_at = excluded.updated_at",
            params![
                session_id,
                cwd,
                project,
                model,
                turn_id,
                effective_status,
                preview,
                now
            ],
        )?;

        let existing_event = if kind.is_some() && payload.is_some() {
            transaction
                .query_row(
                    "SELECT 1 FROM outbox WHERE event_key = ?1 \
                     OR (event_key = ?2 AND session_id = ?3) LIMIT 1",
                    params![event_key, legacy_event_key, session_id],
                    |_| Ok(()),
                )
                .optional()?
                .is_some()
        } else {
            false
        };

        if event_name == "Stop" && payload.is_some() && !existing_event {
            let content = payload
                .as_ref()
                .and_then(|value| value.get("content"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            transaction.execute(
                "INSERT INTO last_reply(\
                     session_id, turn_id, project, model, content, created_at\
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![session_id, turn_id, project, model, content, now],
            )?;
            self.trim_last_replies(&transaction, &session_id)?;
        }

        let mut inserted = false;
        if let (Some(kind), Some(payload)) = (kind, payload) {
            if !existing_event {
                let muted: Option<String> = transaction
                    .query_row(
                        "SELECT value FROM settings WHERE key = 'muted'",
                        [],
                        |row| row.get(0),
                    )
                    .optional()?;
                let initial_state = if muted.as_deref() == Some("1") {
                    "suppressed"
                } else {
                    "pending"
                };
                let next_attempt_at = if kind == "permission_required" && initial_state == "pending"
                {
                    now + PERMISSION_NOTIFICATION_DELAY
                } else {
                    0.0
                };
                let last_error = (initial_state == "suppressed").then_some("notifications muted");
                inserted = transaction.execute(
                    "INSERT OR IGNORE INTO outbox(\
                         event_key, kind, session_id, turn_id, payload_json, state, created_at, \
                         next_attempt_at, last_error\
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                    params![
                        event_key,
                        kind,
                        session_id,
                        turn_id,
                        serde_json::to_string(&payload)?,
                        initial_state,
                        now,
                        next_attempt_at,
                        last_error
                    ],
                )? == 1;
            }
        }
        transaction.commit()?;
        Ok(inserted)
    }

    pub fn get_setting(&self, key: &str) -> StoreResult<Option<String>> {
        Ok(self
            .connect()?
            .query_row("SELECT value FROM settings WHERE key = ?1", [key], |row| {
                row.get(0)
            })
            .optional()?)
    }

    pub fn set_setting(&self, key: &str, value: &str) -> StoreResult<()> {
        self.connect()?.execute(
            "INSERT INTO settings(key, value) VALUES (?1, ?2) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![key, value],
        )?;
        Ok(())
    }

    pub fn delete_settings<I, S>(&self, keys: I) -> StoreResult<()>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let keys: Vec<String> = keys
            .into_iter()
            .map(|key| key.as_ref().to_owned())
            .collect();
        if keys.is_empty() {
            return Ok(());
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        for key in keys {
            transaction.execute("DELETE FROM settings WHERE key = ?1", [key])?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn set_daemon_info(&self, pid: u32, create_time: f64) -> StoreResult<()> {
        self.set_setting(
            "daemon",
            &serde_json::json!({"pid": pid, "create_time": create_time}).to_string(),
        )
    }

    pub fn get_daemon_info(&self) -> StoreResult<Option<(u32, f64)>> {
        let Some(raw) = self.get_setting("daemon")? else {
            return Ok(None);
        };
        let Ok(value) = serde_json::from_str::<Value>(&raw) else {
            return Ok(None);
        };
        let Some(pid) = value.get("pid").and_then(Value::as_u64) else {
            return Ok(None);
        };
        let Some(create_time) = value.get("create_time").and_then(Value::as_f64) else {
            return Ok(None);
        };
        Ok(u32::try_from(pid).ok().map(|pid| (pid, create_time)))
    }

    pub fn clear_daemon_info(&self, pid: u32) -> StoreResult<()> {
        if self
            .get_daemon_info()?
            .is_some_and(|current| current.0 == pid)
        {
            self.delete_settings(["daemon"])?;
        }
        Ok(())
    }

    pub fn list_hosts(&self) -> StoreResult<Vec<HostProcess>> {
        let connection = self.connect()?;
        let mut statement = connection
            .prepare("SELECT pid, create_time, kind FROM hosts ORDER BY last_seen DESC")?;
        Ok(statement
            .query_map([], |row| {
                Ok(HostProcess::new(
                    row.get::<_, u32>(0)?,
                    row.get::<_, f64>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?)
    }

    pub fn remove_hosts(&self, hosts: &[HostProcess]) -> StoreResult<()> {
        if hosts.is_empty() {
            return Ok(());
        }
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        for host in hosts {
            transaction.execute(
                "DELETE FROM hosts WHERE pid = ?1 AND create_time = ?2",
                params![host.pid, host.create_time],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }

    pub fn get_bound_openid(&self) -> StoreResult<Option<String>> {
        self.get_setting("bound_openid")
    }

    pub fn create_pairing(&self, code: &str, expires_at: f64) -> StoreResult<()> {
        self.set_setting("pairing_hash", &hash_pairing_code(code))?;
        self.set_setting("pairing_expires_at", &expires_at.to_string())
    }

    pub fn consume_pairing(&self, code: &str, openid: &str) -> StoreResult<bool> {
        self.consume_pairing_at(code, openid, now_seconds())
    }

    pub fn consume_pairing_at(&self, code: &str, openid: &str, now: f64) -> StoreResult<bool> {
        let supplied = hash_pairing_code(code);
        let mut connection = self.connect()?;
        let transaction = connection.transaction()?;
        let stored_hash: Option<String> = transaction
            .query_row(
                "SELECT value FROM settings WHERE key = 'pairing_hash'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        let expiry: Option<String> = transaction
            .query_row(
                "SELECT value FROM settings WHERE key = 'pairing_expires_at'",
                [],
                |row| row.get(0),
            )
            .optional()?;
        let valid = stored_hash
            .as_deref()
            .zip(
                expiry
                    .as_deref()
                    .and_then(|value| value.parse::<f64>().ok()),
            )
            .is_some_and(|(stored_hash, expiry)| {
                now <= expiry && constant_time_eq(stored_hash.as_bytes(), supplied.as_bytes())
            });
        if !valid {
            return Ok(false);
        }
        transaction.execute(
            "INSERT INTO settings(key, value) VALUES ('bound_openid', ?1) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [openid],
        )?;
        transaction.execute(
            "DELETE FROM settings WHERE key IN ('pairing_hash', 'pairing_expires_at')",
            [],
        )?;
        transaction.execute(
            "UPDATE outbox SET state = 'suppressed', \
             last_error = 'created before QQ binding' \
             WHERE state = 'pending' AND created_at <= ?1",
            [now],
        )?;
        transaction.commit()?;
        Ok(true)
    }

    pub fn pairing_status(&self) -> StoreResult<(bool, Option<f64>)> {
        self.pairing_status_at(now_seconds())
    }

    pub fn pairing_status_at(&self, now: f64) -> StoreResult<(bool, Option<f64>)> {
        if self.get_setting("pairing_hash")?.is_none() {
            return Ok((false, None));
        }
        let expiry = self
            .get_setting("pairing_expires_at")?
            .and_then(|raw| raw.parse::<f64>().ok());
        Ok(match expiry {
            Some(expiry) => (expiry >= now, Some(expiry)),
            None => (false, None),
        })
    }

    pub fn is_muted(&self) -> StoreResult<bool> {
        Ok(self.get_setting("muted")?.as_deref() == Some("1"))
    }

    pub fn set_muted(&self, muted: bool) -> StoreResult<()> {
        self.set_setting("muted", if muted { "1" } else { "0" })
    }

    pub fn remember_inbound(&self, message_id: &str) -> StoreResult<bool> {
        self.remember_inbound_at(message_id, now_seconds())
    }

    pub fn remember_inbound_at(&self, message_id: &str, now: f64) -> StoreResult<bool> {
        Ok(self.connect()?.execute(
            "INSERT OR IGNORE INTO inbound_messages(message_id, created_at) VALUES (?1, ?2)",
            params![message_id, now],
        )? == 1)
    }

    pub fn get_due_outbox(&self) -> StoreResult<Option<OutboxItem>> {
        self.get_due_outbox_at(now_seconds())
    }

    pub fn get_due_outbox_at(&self, now: f64) -> StoreResult<Option<OutboxItem>> {
        let connection = self.connect()?;
        self.cleanup_expired_content(&connection, now)?;
        let raw = connection
            .query_row(
                "SELECT id, event_key, kind, session_id, turn_id, payload_json, \
                 segments_json, segment_index, attempts, created_at FROM outbox \
                 WHERE state = 'pending' AND next_attempt_at <= ?1 ORDER BY id ASC LIMIT 1",
                [now],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                        row.get::<_, String>(3)?,
                        row.get::<_, Option<String>>(4)?,
                        row.get::<_, String>(5)?,
                        row.get::<_, Option<String>>(6)?,
                        row.get::<_, i64>(7)?,
                        row.get::<_, i64>(8)?,
                        row.get::<_, f64>(9)?,
                    ))
                },
            )
            .optional()?;
        let Some((
            id,
            event_key,
            kind,
            session_id,
            turn_id,
            payload_json,
            segments_json,
            segment_index,
            attempts,
            created_at,
        )) = raw
        else {
            return Ok(None);
        };
        Ok(Some(OutboxItem {
            id,
            event_key,
            kind,
            session_id,
            turn_id,
            payload: serde_json::from_str(&payload_json)?,
            segments: segments_json
                .map(|value| serde_json::from_str(&value))
                .transpose()?,
            segment_index: segment_index.max(0) as usize,
            attempts: attempts.max(0) as u32,
            created_at,
        }))
    }

    pub fn has_pending_outbox(&self) -> StoreResult<bool> {
        Ok(self
            .connect()?
            .query_row(
                "SELECT 1 FROM outbox WHERE state = 'pending' LIMIT 1",
                [],
                |_| Ok(()),
            )
            .optional()?
            .is_some())
    }

    pub fn companion_work_pending(&self) -> StoreResult<bool> {
        if self.pairing_status()?.0 {
            return Ok(true);
        }
        Ok(self.get_bound_openid()?.is_some() && self.has_pending_outbox()?)
    }

    pub fn prepare_segments(&self, item_id: i64, segments: &[String]) -> StoreResult<()> {
        self.connect()?.execute(
            "UPDATE outbox SET segments_json = ?1, segment_index = 0 WHERE id = ?2",
            params![serde_json::to_string(segments)?, item_id],
        )?;
        Ok(())
    }

    pub fn replace_segments(
        &self,
        item_id: i64,
        segments: &[String],
        index: usize,
    ) -> StoreResult<()> {
        self.connect()?.execute(
            "UPDATE outbox SET segments_json = ?1, segment_index = ?2 WHERE id = ?3",
            params![serde_json::to_string(segments)?, index as i64, item_id],
        )?;
        Ok(())
    }

    pub fn advance_segment(
        &self,
        item_id: i64,
        current_index: usize,
        total: usize,
    ) -> StoreResult<()> {
        let connection = self.connect()?;
        if current_index + 1 >= total {
            connection.execute(
                "UPDATE outbox SET state = 'delivered', segment_index = ?1, \
                 last_error = NULL WHERE id = ?2",
                params![total as i64, item_id],
            )?;
        } else {
            connection.execute(
                "UPDATE outbox SET segment_index = ?1, attempts = 0, \
                 next_attempt_at = 0, last_error = NULL WHERE id = ?2",
                params![(current_index + 1) as i64, item_id],
            )?;
        }
        Ok(())
    }

    pub fn reschedule(&self, item_id: i64, delay: f64, error: &str) -> StoreResult<()> {
        self.reschedule_at(item_id, delay, error, now_seconds())
    }

    pub fn reschedule_at(
        &self,
        item_id: i64,
        delay: f64,
        error: &str,
        now: f64,
    ) -> StoreResult<()> {
        let safe_error: String = error.chars().take(500).collect();
        self.connect()?.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_attempt_at = ?1, \
             last_error = ?2, state = 'pending' WHERE id = ?3",
            params![now + delay, safe_error, item_id],
        )?;
        Ok(())
    }

    pub fn mark_outbox(&self, item_id: i64, state: &str, reason: &str) -> StoreResult<()> {
        if !matches!(state, "delivered" | "suppressed" | "failed_permanent") {
            return Err(StoreError::InvalidOutboxState(state.to_owned()));
        }
        let reason: String = reason.chars().take(500).collect();
        self.connect()?.execute(
            "UPDATE outbox SET state = ?1, last_error = ?2 WHERE id = ?3",
            params![state, reason, item_id],
        )?;
        Ok(())
    }

    fn session_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<SessionInfo> {
        Ok(SessionInfo {
            session_id: row.get("session_id")?,
            cwd: row.get("cwd")?,
            project: row.get("project")?,
            model: row.get("model")?,
            turn_id: row.get("turn_id")?,
            status: row.get("status")?,
            prompt_preview: row.get("prompt_preview")?,
            updated_at: row.get("updated_at")?,
        })
    }

    pub fn get_sessions_for_status(&self) -> StoreResult<Vec<SessionInfo>> {
        let connection = self.connect()?;
        let active = {
            let mut statement = connection.prepare(
                "SELECT * FROM sessions WHERE status IN ('running', 'awaiting_approval', 'idle') \
                 ORDER BY updated_at DESC LIMIT 3",
            )?;
            statement
                .query_map([], Self::session_from_row)?
                .collect::<Result<Vec<_>, _>>()?
        };
        if !active.is_empty() {
            return Ok(active);
        }
        let mut statement =
            connection.prepare("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1")?;
        Ok(statement
            .query_map([], Self::session_from_row)?
            .collect::<Result<Vec<_>, _>>()?)
    }

    fn last_reply_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<LastReply> {
        Ok(LastReply {
            reply_id: row.get("reply_id")?,
            session_id: row.get("session_id")?,
            turn_id: row.get("turn_id")?,
            project: row.get("project")?,
            model: row.get("model")?,
            content: row.get("content")?,
            created_at: row.get("created_at")?,
        })
    }

    pub fn get_last_reply(
        &self,
        project: Option<&str>,
        session_id: Option<&str>,
    ) -> StoreResult<Option<LastReply>> {
        self.get_last_reply_at(project, session_id, now_seconds())
    }

    pub fn get_last_reply_at(
        &self,
        project: Option<&str>,
        session_id: Option<&str>,
        now: f64,
    ) -> StoreResult<Option<LastReply>> {
        let connection = self.connect()?;
        self.cleanup_expired_content(&connection, now)?;
        let result = match (project, session_id) {
            (Some(project), Some(session_id)) => connection
                .query_row(
                    "SELECT * FROM last_reply WHERE project = ?1 COLLATE NOCASE \
                     AND session_id = ?2 ORDER BY created_at DESC, reply_id DESC LIMIT 1",
                    params![project, session_id],
                    Self::last_reply_from_row,
                )
                .optional()?,
            (Some(project), None) => connection
                .query_row(
                    "SELECT * FROM last_reply WHERE project = ?1 COLLATE NOCASE \
                     ORDER BY created_at DESC, reply_id DESC LIMIT 1",
                    [project],
                    Self::last_reply_from_row,
                )
                .optional()?,
            (None, Some(session_id)) => connection
                .query_row(
                    "SELECT * FROM last_reply WHERE session_id = ?1 \
                     ORDER BY created_at DESC, reply_id DESC LIMIT 1",
                    [session_id],
                    Self::last_reply_from_row,
                )
                .optional()?,
            (None, None) => connection
                .query_row(
                    "SELECT * FROM last_reply ORDER BY created_at DESC, reply_id DESC LIMIT 1",
                    [],
                    Self::last_reply_from_row,
                )
                .optional()?,
        };
        Ok(result)
    }

    pub fn get_last_replies(
        &self,
        project: Option<&str>,
        session_id: Option<&str>,
        limit: Option<usize>,
    ) -> StoreResult<Vec<LastReply>> {
        self.get_last_replies_at(project, session_id, limit, now_seconds())
    }

    pub fn get_last_replies_at(
        &self,
        project: Option<&str>,
        session_id: Option<&str>,
        limit: Option<usize>,
        now: f64,
    ) -> StoreResult<Vec<LastReply>> {
        let limit = limit.unwrap_or(self.last_reply_limit).clamp(1, 1_000) as i64;
        let connection = self.connect()?;
        self.cleanup_expired_content(&connection, now)?;
        let (sql, first, second): (&str, Option<&str>, Option<&str>) = match (project, session_id) {
            (Some(project), Some(session_id)) => (
                "SELECT * FROM last_reply WHERE project = ?1 COLLATE NOCASE AND session_id = ?2 \
                 ORDER BY created_at DESC, reply_id DESC LIMIT ?3",
                Some(project),
                Some(session_id),
            ),
            (Some(project), None) => (
                "SELECT * FROM last_reply WHERE project = ?1 COLLATE NOCASE \
                 ORDER BY created_at DESC, reply_id DESC LIMIT ?2",
                Some(project),
                None,
            ),
            (None, Some(session_id)) => (
                "SELECT * FROM last_reply WHERE session_id = ?1 \
                 ORDER BY created_at DESC, reply_id DESC LIMIT ?2",
                Some(session_id),
                None,
            ),
            (None, None) => (
                "SELECT * FROM last_reply ORDER BY created_at DESC, reply_id DESC LIMIT ?1",
                None,
                None,
            ),
        };
        let mut statement = connection.prepare(sql)?;
        let replies = match (first, second) {
            (Some(first), Some(second)) => statement
                .query_map(params![first, second, limit], Self::last_reply_from_row)?
                .collect::<Result<Vec<_>, _>>()?,
            (Some(first), None) => statement
                .query_map(params![first, limit], Self::last_reply_from_row)?
                .collect::<Result<Vec<_>, _>>()?,
            (None, None) => statement
                .query_map([limit], Self::last_reply_from_row)?
                .collect::<Result<Vec<_>, _>>()?,
            (None, Some(_)) => unreachable!(),
        };
        Ok(replies)
    }

    pub fn get_last_reply_projects(&self) -> StoreResult<Vec<String>> {
        self.get_last_reply_projects_at(now_seconds())
    }

    pub fn get_last_reply_projects_at(&self, now: f64) -> StoreResult<Vec<String>> {
        let connection = self.connect()?;
        self.cleanup_expired_content(&connection, now)?;
        let mut statement = connection.prepare(
            "SELECT project, MAX(created_at) AS latest FROM last_reply \
             GROUP BY project ORDER BY latest DESC, project COLLATE NOCASE",
        )?;
        Ok(statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<Result<Vec<_>, _>>()?)
    }

    pub fn cleanup(&self) -> StoreResult<()> {
        self.cleanup_at(now_seconds())
    }

    pub fn cleanup_at(&self, now: f64) -> StoreResult<()> {
        let connection = self.connect()?;
        self.cleanup_expired_content(&connection, now)?;
        connection.execute(
            "DELETE FROM inbound_messages WHERE created_at < ?1",
            [now - 7.0 * 24.0 * 60.0 * 60.0],
        )?;
        if !Self::permission_notifications_enabled() {
            connection.execute(
                "UPDATE outbox SET state = 'suppressed', last_error = ?1 \
                 WHERE state = 'pending' AND kind = 'permission_required'",
                ["permission notifications disabled"],
            )?;
        }
        Ok(())
    }
}

impl DaemonState for Store {
    type Error = StoreError;

    fn get_daemon_info(&self) -> Result<Option<(u32, f64)>, Self::Error> {
        Store::get_daemon_info(self)
    }

    fn set_daemon_info(&self, pid: u32, create_time: f64) -> Result<(), Self::Error> {
        Store::set_daemon_info(self, pid, create_time)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    fn event(name: &str) -> Value {
        serde_json::json!({
            "hook_event_name": name,
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": r"D:\work\示例项目",
            "model": "gpt-5.6-codex",
        })
    }

    #[test]
    fn final_replies_are_deduplicated_and_retained() {
        let root = tempdir().unwrap();
        let store = Store::new(root.path().join("state.sqlite3")).unwrap();
        let mut final_event = event("Stop");
        final_event["last_assistant_message"] = Value::String("完整内容🙂".to_owned());
        assert!(store.ingest_hook(&final_event, None).unwrap());
        assert!(!store.ingest_hook(&final_event, None).unwrap());
        assert_eq!(
            store.get_last_reply(None, None).unwrap().unwrap().content,
            "完整内容🙂"
        );
    }

    #[test]
    fn same_raw_session_in_different_projects_is_isolated() {
        let first = Store::scoped_session_id("same", r"D:\work\one", None);
        let second = Store::scoped_session_id("same", r"E:\work\two", None);
        assert_ne!(first, second);
        assert_eq!(Store::project_name(r"D:\work\示例项目"), "示例项目");
    }

    #[test]
    fn pairing_is_consumed_once_and_suppresses_prebinding_messages() {
        let root = tempdir().unwrap();
        let store = Store::new(root.path().join("state.sqlite3")).unwrap();
        store.create_pairing("ABCD-EF23", 200.0).unwrap();
        assert!(
            store
                .consume_pairing_at("abcd ef23", "openid", 100.0)
                .unwrap()
        );
        assert!(
            !store
                .consume_pairing_at("ABCD-EF23", "other", 100.0)
                .unwrap()
        );
        assert_eq!(store.get_bound_openid().unwrap().as_deref(), Some("openid"));
    }
}
