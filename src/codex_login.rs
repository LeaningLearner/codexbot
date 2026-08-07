//! JSONL JSON-RPC client for Codex app-server account endpoints.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::ffi::OsString;
use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, Lines};
use tokio::process::{Child, ChildStdin, ChildStdout, Command};
use tokio::sync::{Mutex, OwnedMutexGuard, oneshot};
use tokio::task::JoinHandle;
use tokio::time::{Instant, timeout, timeout_at};

use crate::security::redact_secrets;

pub const APP_SERVER_DASHBOARD_URL: &str = "https://chatgpt.com/codex/settings/usage";
pub const DEFAULT_APP_SERVER_TIMEOUT: Duration = Duration::from_secs(20);
pub const DEFAULT_DEVICE_LOGIN_TIMEOUT: Duration = Duration::from_secs(10 * 60);
pub const DEFAULT_DEVICE_LOGIN_POLL_INTERVAL: Duration = Duration::from_secs(1);

fn safe_detail(value: impl fmt::Display, limit: usize) -> String {
    redact_secrets(&value.to_string())
        .replace(['\r', '\n'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(limit)
        .collect()
}

#[derive(Debug, Error)]
pub enum AppServerError {
    #[error("{0}")]
    Unavailable(String),
    #[error("{0}")]
    Timeout(String),
    #[error("{0}")]
    Protocol(String),
    #[error("RPC error {code:?}: {message}")]
    Rpc {
        code: Option<i64>,
        message: String,
        data_summary: String,
    },
    #[error("{0}")]
    LoginCancelled(String),
    #[error("{0}")]
    LoginFailed(String),
    #[error("已有 Codex 账号切换正在进行")]
    LoginInProgress,
}

impl AppServerError {
    pub fn rpc_code(&self) -> Option<i64> {
        match self {
            Self::Rpc { code, .. } => *code,
            _ => None,
        }
    }

    pub fn is_timeout(&self) -> bool {
        matches!(self, Self::Timeout(_))
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AccountInfo {
    pub email: Option<String>,
    pub plan: Option<String>,
    pub auth_type: String,
    pub requires_openai_auth: bool,
}

impl AccountInfo {
    pub fn is_authenticated(&self) -> bool {
        let has_identity = self.email.is_some()
            || self.plan.is_some()
            || !matches!(self.auth_type.as_str(), "" | "unknown" | "not_logged_in");
        has_identity && !(self.requires_openai_auth && self.auth_type == "not_logged_in")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceLoginStart {
    pub login_id: String,
    pub verification_url: String,
    pub user_code: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeviceLoginResult {
    pub started: DeviceLoginStart,
    pub completed: bool,
    pub account: Option<AccountInfo>,
    pub error: Option<String>,
    pub cancelled: bool,
}

fn first_text(values: impl IntoIterator<Item = Option<String>>) -> Option<String> {
    values
        .into_iter()
        .flatten()
        .map(|value| value.trim().to_owned())
        .find(|value| !value.is_empty())
}

fn value_text(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::Null => None,
        Value::String(value) => Some(value.clone()),
        value => Some(value.to_string()),
    }
}

pub fn parse_account_result(payload: &Value) -> AccountInfo {
    let Some(root) = payload.as_object() else {
        return AccountInfo {
            auth_type: "unknown".to_owned(),
            ..AccountInfo::default()
        };
    };
    let account_value = root.get("account").unwrap_or(payload);
    let account = account_value.as_object();
    let requires_openai_auth = root
        .get("requiresOpenaiAuth")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let Some(account) = account.filter(|account| !account.is_empty()) else {
        return AccountInfo {
            email: None,
            plan: None,
            auth_type: if requires_openai_auth {
                "not_logged_in".to_owned()
            } else {
                "unknown".to_owned()
            },
            requires_openai_auth,
        };
    };
    AccountInfo {
        email: first_text([
            value_text(account.get("email")),
            value_text(account.get("emailAddress")),
        ]),
        plan: first_text([
            value_text(account.get("planType")),
            value_text(account.get("plan")),
            value_text(account.get("subscriptionType")),
        ]),
        auth_type: first_text([
            value_text(account.get("authType")),
            value_text(account.get("auth_type")),
            value_text(account.get("type")),
            value_text(root.get("authType")),
        ])
        .unwrap_or_else(|| "unknown".to_owned()),
        requires_openai_auth,
    }
}

pub fn parse_device_login_result(payload: &Value) -> Result<DeviceLoginStart, AppServerError> {
    let result = payload.as_object().ok_or_else(|| {
        AppServerError::Protocol(
            "account/login/start did not return a complete device code".to_owned(),
        )
    })?;
    let login_type = value_text(result.get("type"));
    let login_id = first_text([
        value_text(result.get("loginId")),
        value_text(result.get("login_id")),
    ]);
    let verification_url = first_text([
        value_text(result.get("verificationUrl")),
        value_text(result.get("verification_url")),
    ]);
    let user_code = first_text([
        value_text(result.get("userCode")),
        value_text(result.get("user_code")),
    ]);
    match (login_type, login_id, verification_url, user_code) {
        (Some(_), Some(login_id), Some(verification_url), Some(user_code)) => {
            Ok(DeviceLoginStart {
                login_id,
                verification_url,
                user_code,
            })
        }
        _ => Err(AppServerError::Protocol(
            "account/login/start did not return a complete device code".to_owned(),
        )),
    }
}

#[derive(Clone)]
pub struct CodexAppServerClient {
    command: Option<Vec<OsString>>,
    pub timeout: Duration,
    rpc_lock: Arc<Mutex<()>>,
}

impl fmt::Debug for CodexAppServerClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexAppServerClient")
            .field("command", &self.command)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

impl Default for CodexAppServerClient {
    fn default() -> Self {
        Self::new(None, DEFAULT_APP_SERVER_TIMEOUT)
    }
}

impl CodexAppServerClient {
    pub fn new(command: Option<Vec<OsString>>, timeout: Duration) -> Self {
        Self {
            command,
            timeout: timeout.max(Duration::from_millis(100)),
            rpc_lock: Arc::new(Mutex::new(())),
        }
    }

    pub fn with_command(command: impl Into<OsString>) -> Self {
        Self::new(Some(vec![command.into()]), DEFAULT_APP_SERVER_TIMEOUT)
    }

    fn command(&self) -> Result<Vec<OsString>, AppServerError> {
        if let Some(command) = self.command.clone().filter(|command| !command.is_empty()) {
            return Ok(command);
        }
        if let Some(command) = std::env::var_os("CODEX_COMMAND").filter(|value| !value.is_empty()) {
            return Ok(vec![command]);
        }
        #[cfg(windows)]
        let candidates = ["codex.cmd", "codex.exe", "codex"];
        #[cfg(not(windows))]
        let candidates = ["codex", "codex", "codex"];
        for candidate in candidates {
            if command_exists(candidate) {
                return Ok(vec![OsString::from(candidate)]);
            }
        }
        Err(AppServerError::Unavailable(
            "找不到 codex/codex.cmd".to_owned(),
        ))
    }

    pub async fn open_session(
        &self,
        request_timeout: Option<Duration>,
    ) -> Result<CodexAppServerSession, AppServerError> {
        let request_timeout = request_timeout
            .unwrap_or(self.timeout)
            .max(Duration::from_millis(100));
        let guard = timeout(request_timeout, self.rpc_lock.clone().lock_owned())
            .await
            .map_err(|_| {
                AppServerError::Timeout("Codex app-server 正在处理另一个请求".to_owned())
            })?;
        let mut command_line = self.command()?;
        let program = command_line.remove(0);
        let mut command = Command::new(program);
        command
            .args(command_line)
            .arg("app-server")
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .kill_on_drop(true);
        #[cfg(windows)]
        {
            use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command.spawn().map_err(|error| {
            AppServerError::Unavailable(format!("无法启动 Codex app-server：{}", error.kind()))
        })?;
        let stdin = child.stdin.take().ok_or_else(|| {
            AppServerError::Unavailable("Codex app-server 未提供 stdin".to_owned())
        })?;
        let stdout = child.stdout.take().ok_or_else(|| {
            AppServerError::Unavailable("Codex app-server 未提供 stdout".to_owned())
        })?;
        let mut session = CodexAppServerSession {
            child,
            stdin,
            lines: BufReader::new(stdout).lines(),
            pending: HashMap::new(),
            notifications: Vec::new(),
            next_id: 0,
            timeout: request_timeout,
            _guard: guard,
        };
        session.initialize().await?;
        Ok(session)
    }

    pub async fn call(
        &self,
        method: &str,
        params: Value,
        request_timeout: Option<Duration>,
    ) -> Result<Value, AppServerError> {
        let mut session = self.open_session(request_timeout).await?;
        session.request(method, params, request_timeout).await
    }

    pub async fn read_account(&self) -> Result<AccountInfo, AppServerError> {
        let value = self
            .call("account/read", json!({"refreshToken": false}), None)
            .await?;
        Ok(parse_account_result(&value))
    }

    pub async fn account_read(&self) -> Result<AccountInfo, AppServerError> {
        self.read_account().await
    }

    pub async fn read_rate_limits(&self) -> Result<Value, AppServerError> {
        let value = self
            .call("account/rateLimits/read", json!({}), None)
            .await?;
        if !value.is_object() {
            return Err(AppServerError::Protocol(
                "account/rateLimits/read returned an invalid result".to_owned(),
            ));
        }
        Ok(value)
    }

    pub async fn rate_limits_read(&self) -> Result<Value, AppServerError> {
        self.read_rate_limits().await
    }

    pub async fn start_device_login_session(
        &self,
    ) -> Result<(CodexAppServerSession, DeviceLoginStart), AppServerError> {
        let mut session = self.open_session(None).await?;
        let result = session
            .request(
                "account/login/start",
                json!({"type": "chatgptDeviceCode"}),
                None,
            )
            .await?;
        let start = parse_device_login_result(&result)?;
        Ok((session, start))
    }
}

fn command_exists(command: &str) -> bool {
    let command = PathBuf::from(command);
    if command.components().count() > 1 {
        return command.is_file();
    }
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|directory| directory.join(&command).is_file())
}

pub struct CodexAppServerSession {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    pending: HashMap<u64, Value>,
    notifications: Vec<Value>,
    next_id: u64,
    timeout: Duration,
    _guard: OwnedMutexGuard<()>,
}

impl fmt::Debug for CodexAppServerSession {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexAppServerSession")
            .field("next_id", &self.next_id)
            .field("timeout", &self.timeout)
            .finish_non_exhaustive()
    }
}

impl CodexAppServerSession {
    async fn send(&mut self, message: &Value) -> Result<(), AppServerError> {
        let mut encoded = serde_json::to_vec(message).map_err(|_| {
            AppServerError::Protocol("Codex app-server request is not JSON".to_owned())
        })?;
        encoded.push(b'\n');
        self.stdin.write_all(&encoded).await.map_err(|error| {
            AppServerError::Unavailable(format!("Codex app-server stdin 失败：{}", error.kind()))
        })?;
        self.stdin.flush().await.map_err(|error| {
            AppServerError::Unavailable(format!("Codex app-server stdin 失败：{}", error.kind()))
        })
    }

    async fn read_message(
        &mut self,
        deadline: Instant,
    ) -> Result<Map<String, Value>, AppServerError> {
        loop {
            let line = timeout_at(deadline, self.lines.next_line())
                .await
                .map_err(|_| AppServerError::Timeout("Codex app-server 响应超时".to_owned()))?
                .map_err(|error| {
                    AppServerError::Unavailable(format!(
                        "Codex app-server stdout 失败：{}",
                        error.kind()
                    ))
                })?
                .ok_or_else(|| {
                    AppServerError::Unavailable("Codex app-server 提前退出".to_owned())
                })?;
            let Ok(Value::Object(message)) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            return Ok(message);
        }
    }

    async fn response_for(
        &mut self,
        request_id: u64,
        deadline: Instant,
    ) -> Result<Value, AppServerError> {
        let mut response = self.pending.remove(&request_id);
        while response.is_none() {
            let message = self.read_message(deadline).await?;
            if !message.contains_key("id") {
                if message.contains_key("method") {
                    self.notifications.push(Value::Object(message));
                }
                continue;
            }
            let response_id = message.get("id").and_then(Value::as_u64);
            if message.contains_key("method")
                && !message.contains_key("result")
                && !message.contains_key("error")
            {
                continue;
            }
            if response_id != Some(request_id) {
                if let Some(response_id) = response_id {
                    self.pending.insert(response_id, Value::Object(message));
                }
                continue;
            }
            response = Some(Value::Object(message));
        }

        let response = response
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| {
                AppServerError::Protocol("Codex app-server response is not an object".to_owned())
            })?;
        if let Some(error) = response.get("error").and_then(Value::as_object) {
            let code = error.get("code").and_then(|value| {
                value
                    .as_i64()
                    .or_else(|| value.as_str()?.parse::<i64>().ok())
            });
            return Err(AppServerError::Rpc {
                code,
                message: safe_detail(
                    error
                        .get("message")
                        .map(Value::to_string)
                        .unwrap_or_default(),
                    240,
                ),
                data_summary: error
                    .get("data")
                    .map(|value| safe_detail(value, 240))
                    .unwrap_or_default(),
            });
        }
        response.get("result").cloned().ok_or_else(|| {
            AppServerError::Protocol("Codex app-server response missing result".to_owned())
        })
    }

    async fn raw_request(
        &mut self,
        method: &str,
        params: Value,
        deadline: Instant,
    ) -> Result<Value, AppServerError> {
        let request_id = self.next_id;
        self.next_id += 1;
        if !self.pending.contains_key(&request_id) {
            self.send(&json!({
                "method": method,
                "id": request_id,
                "params": params,
            }))
            .await?;
        }
        self.response_for(request_id, deadline).await
    }

    async fn initialize(&mut self) -> Result<(), AppServerError> {
        let deadline = Instant::now() + self.timeout;
        self.raw_request(
            "initialize",
            json!({
                "clientInfo": {
                    "name": "codexbot",
                    "title": "CodexBot",
                    "version": env!("CARGO_PKG_VERSION")
                }
            }),
            deadline,
        )
        .await?;
        self.send(&json!({"method": "initialized", "params": {}}))
            .await
    }

    pub async fn request(
        &mut self,
        method: &str,
        params: Value,
        request_timeout: Option<Duration>,
    ) -> Result<Value, AppServerError> {
        let request_timeout = request_timeout
            .unwrap_or(self.timeout)
            .max(Duration::from_millis(100));
        self.raw_request(method, params, Instant::now() + request_timeout)
            .await
    }

    fn notification_login_result(
        message: &Value,
        login_id: &str,
    ) -> Result<Option<Option<AccountInfo>>, AppServerError> {
        let Some(message) = message.as_object() else {
            return Ok(None);
        };
        if message
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_lowercase()
            != "account/login/completed"
        {
            return Ok(None);
        }
        let params = message
            .get("params")
            .and_then(Value::as_object)
            .cloned()
            .unwrap_or_default();
        if params
            .get("loginId")
            .map(Value::to_string)
            .map(|value| value.trim_matches('"').to_owned())
            .is_some_and(|value| value != login_id)
        {
            return Ok(None);
        }
        let success = params
            .get("success")
            .and_then(Value::as_bool)
            .ok_or_else(|| {
                AppServerError::Protocol(
                    "account/login/completed missing boolean success".to_owned(),
                )
            })?;
        if !success {
            let detail = params
                .get("error")
                .map(|value| safe_detail(value, 240))
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| "设备码登录失败".to_owned());
            return Err(AppServerError::LoginFailed(detail));
        }
        Ok(Some(params.get("account").map(parse_account_result)))
    }

    fn take_login_notification(
        &mut self,
        login_id: &str,
    ) -> Result<Option<Option<AccountInfo>>, AppServerError> {
        let mut remaining = Vec::new();
        let mut result = None;
        for message in std::mem::take(&mut self.notifications) {
            if result.is_none() {
                if let Some(candidate) = Self::notification_login_result(&message, login_id)? {
                    result = Some(candidate);
                    continue;
                }
            }
            remaining.push(message);
        }
        self.notifications = remaining;
        Ok(result)
    }

    pub async fn wait_for_login(
        &mut self,
        login_id: &str,
        login_timeout: Duration,
        mut cancel: oneshot::Receiver<()>,
    ) -> Result<Option<AccountInfo>, AppServerError> {
        let deadline = Instant::now() + login_timeout.max(Duration::from_millis(100));
        loop {
            if let Some(result) = self.take_login_notification(login_id)? {
                return Ok(result);
            }
            let message = tokio::select! {
                _ = &mut cancel => {
                    return Err(AppServerError::LoginCancelled("设备码登录已取消".to_owned()));
                }
                result = self.read_message(deadline) => result?,
            };
            if let Some(response_id) = message.get("id").and_then(Value::as_u64) {
                if !message.contains_key("method")
                    || message.contains_key("result")
                    || message.contains_key("error")
                {
                    self.pending.insert(response_id, Value::Object(message));
                }
            } else if message.contains_key("method") {
                self.notifications.push(Value::Object(message));
            }
        }
    }

    pub async fn close(&mut self) {
        let _ = self.stdin.shutdown().await;
        if matches!(self.child.try_wait(), Ok(None)) {
            let _ = self.child.start_kill();
            let _ = timeout(Duration::from_secs(1), self.child.wait()).await;
        }
    }
}

impl Drop for CodexAppServerSession {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
    }
}

type Completion = Box<dyn FnOnce(DeviceLoginResult) + Send + 'static>;

#[derive(Clone)]
pub struct CodexLoginService {
    client: CodexAppServerClient,
    login_timeout: Duration,
    active: Arc<Mutex<Option<ActiveLogin>>>,
}

struct ActiveLogin {
    cancel: Option<oneshot::Sender<()>>,
    worker: JoinHandle<()>,
}

impl fmt::Debug for CodexLoginService {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexLoginService")
            .field("login_timeout", &self.login_timeout)
            .finish_non_exhaustive()
    }
}

impl Default for CodexLoginService {
    fn default() -> Self {
        Self::new(
            CodexAppServerClient::default(),
            DEFAULT_DEVICE_LOGIN_TIMEOUT,
        )
    }
}

impl CodexLoginService {
    pub fn new(client: CodexAppServerClient, login_timeout: Duration) -> Self {
        Self {
            client,
            login_timeout: login_timeout.max(Duration::from_millis(100)),
            active: Arc::new(Mutex::new(None)),
        }
    }

    pub async fn start_device_login<F>(
        &self,
        on_complete: Option<F>,
    ) -> Result<DeviceLoginStart, AppServerError>
    where
        F: FnOnce(DeviceLoginResult) + Send + 'static,
    {
        let mut active = self.active.lock().await;
        if active
            .as_ref()
            .is_some_and(|item| !item.worker.is_finished())
        {
            return Err(AppServerError::LoginInProgress);
        }
        if let Some(previous) = active.take() {
            let _ = previous.worker.await;
        }
        let (mut session, start) = self.client.start_device_login_session().await?;
        let (cancel_tx, cancel_rx) = oneshot::channel();
        let started = start.clone();
        let timeout = self.login_timeout;
        let active_state = self.active.clone();
        let completion: Option<Completion> =
            on_complete.map(|callback| Box::new(callback) as Completion);
        let worker = tokio::spawn(async move {
            let outcome = session
                .wait_for_login(&started.login_id, timeout, cancel_rx)
                .await;
            session.close().await;
            let result = match outcome {
                Ok(account) => DeviceLoginResult {
                    started,
                    completed: true,
                    account,
                    error: None,
                    cancelled: false,
                },
                Err(AppServerError::LoginCancelled(error)) => DeviceLoginResult {
                    started,
                    completed: false,
                    account: None,
                    error: Some(error),
                    cancelled: true,
                },
                Err(error) => DeviceLoginResult {
                    started,
                    completed: false,
                    account: None,
                    error: Some(safe_detail(error, 240)),
                    cancelled: false,
                },
            };
            if let Some(completion) = completion {
                completion(result);
            }
            // Avoid awaiting our own JoinHandle. Clearing is best effort;
            // the next starter also treats a finished handle as inactive.
            if let Ok(mut active) = active_state.try_lock() {
                if active
                    .as_ref()
                    .is_some_and(|item| item.worker.is_finished())
                {
                    *active = None;
                }
            }
        });
        *active = Some(ActiveLogin {
            cancel: Some(cancel_tx),
            worker,
        });
        Ok(start)
    }

    pub async fn cancel_device_login(&self) -> bool {
        let mut active = self.active.lock().await;
        active
            .as_mut()
            .and_then(|item| item.cancel.take())
            .is_some_and(|cancel| cancel.send(()).is_ok())
    }

    pub async fn close(&self, wait: Duration) -> bool {
        let cancelled = self.cancel_device_login().await;
        let active = self.active.lock().await.take();
        if let Some(active) = active {
            let mut worker = active.worker;
            if timeout(wait, &mut worker).await.is_err() {
                worker.abort();
            }
        }
        cancelled
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn account_with_identity_remains_authenticated_when_auth_is_required() {
        let account = parse_account_result(&json!({
            "account": {
                "email": "owner@example.com",
                "planType": "plus",
                "type": "chatgpt"
            },
            "requiresOpenaiAuth": true
        }));
        assert!(account.is_authenticated());
        assert_eq!(account.email.as_deref(), Some("owner@example.com"));
    }

    #[test]
    fn device_login_requires_complete_result() {
        assert!(parse_device_login_result(&json!({"loginId": "x"})).is_err());
    }
}
