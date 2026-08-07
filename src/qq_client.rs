//! Native QQ Bot sandbox HTTP/WebSocket runtime.

use futures_util::{SinkExt, StreamExt};
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::{Value, json};
use std::error::Error as StdError;
use std::fmt;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use thiserror::Error;
use tokio::sync::{Mutex, Notify};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;

use crate::commands::CommandService;
use crate::delivery::{DeliveryOutcome, RateLimiter, deliver_item};
use crate::logging_utils::ProcessSafeLogger;
use crate::processes::process_matches;
use crate::security::{Credentials, redact_secrets};
use crate::store::{Store, StoreError};

pub const HOSTLESS_STARTUP_GRACE: Duration = Duration::from_secs(15);
pub const DEFAULT_MONITOR_INTERVAL: Duration = Duration::from_secs(1);
pub const DEFAULT_SHUTDOWN_DRAIN_TIMEOUT: Duration = Duration::from_secs(60);
pub const DEFAULT_RECONNECT_DELAY: Duration = Duration::from_secs(3);
pub const QQ_TOKEN_ENDPOINT: &str = "https://bots.qq.com/app/getAppAccessToken";
pub const QQ_API_BASE: &str = "https://sandbox.api.sgroup.qq.com";
const QQ_C2C_INTENT: u64 = 1 << 25;

#[derive(Debug, Clone)]
pub struct QQRuntimeOptions {
    pub standalone: bool,
    pub initial_reconnect_delay: Duration,
    pub monitor_interval: Duration,
    pub empty_host_checks: u32,
    pub hostless_startup_grace: Duration,
    pub shutdown_drain_timeout: Duration,
}

impl Default for QQRuntimeOptions {
    fn default() -> Self {
        Self {
            standalone: false,
            initial_reconnect_delay: DEFAULT_RECONNECT_DELAY,
            monitor_interval: DEFAULT_MONITOR_INTERVAL,
            empty_host_checks: 2,
            hostless_startup_grace: HOSTLESS_STARTUP_GRACE,
            shutdown_drain_timeout: DEFAULT_SHUTDOWN_DRAIN_TIMEOUT,
        }
    }
}

#[derive(Debug, Error)]
pub enum QQRuntimeError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("QQ HTTP request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("QQ websocket failed: {0}")]
    WebSocket(#[source] Box<tokio_tungstenite::tungstenite::Error>),
    #[error("QQ protocol error: {0}")]
    Protocol(String),
    #[error("QQ API error: {0}")]
    Api(#[from] QQApiError),
}

impl From<tokio_tungstenite::tungstenite::Error> for QQRuntimeError {
    fn from(error: tokio_tungstenite::tungstenite::Error) -> Self {
        Self::WebSocket(Box::new(error))
    }
}

#[derive(Debug, Clone)]
pub struct QQApiError {
    pub code: Option<i64>,
    pub status: Option<u16>,
    message: String,
}

impl fmt::Display for QQApiError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "code={} status={} {}",
            self.code.map(|value| value.to_string()).unwrap_or_default(),
            self.status
                .map(|value| value.to_string())
                .unwrap_or_default(),
            self.message
        )
    }
}

impl StdError for QQApiError {}

#[derive(Debug, Clone)]
struct AccessToken {
    value: String,
    refresh_at: Instant,
}

#[derive(Clone)]
struct QQApiClient {
    http: reqwest::Client,
    credentials: Credentials,
    token: Arc<Mutex<Option<AccessToken>>>,
}

impl fmt::Debug for QQApiClient {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("QQApiClient")
            .field("app_id", &self.credentials.app_id)
            .field("app_secret", &"[REDACTED]")
            .finish_non_exhaustive()
    }
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    #[serde(alias = "accessToken")]
    access_token: String,
    #[serde(alias = "expiresIn")]
    expires_in: Value,
}

impl QQApiClient {
    fn new(credentials: Credentials) -> Result<Self, QQRuntimeError> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(8))
            .user_agent(concat!("CodexBot/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Self {
            http,
            credentials,
            token: Arc::new(Mutex::new(None)),
        })
    }

    async fn access_token(&self) -> Result<String, QQRuntimeError> {
        let mut token = self.token.lock().await;
        if let Some(token) = token
            .as_ref()
            .filter(|token| token.refresh_at > Instant::now())
        {
            return Ok(token.value.clone());
        }
        let response = self
            .http
            .post(QQ_TOKEN_ENDPOINT)
            .json(&json!({
                "appId": self.credentials.app_id,
                "clientSecret": self.credentials.app_secret,
            }))
            .send()
            .await?;
        let status = response.status();
        let body = response.bytes().await?;
        if !status.is_success() {
            return Err(QQApiError::from_response(status, &body).into());
        }
        let response: TokenResponse = serde_json::from_slice(&body).map_err(|_| {
            QQRuntimeError::Protocol("access-token response is not valid JSON".to_owned())
        })?;
        if response.access_token.trim().is_empty() {
            return Err(QQRuntimeError::Protocol(
                "access-token response did not include a token".to_owned(),
            ));
        }
        let expires = response
            .expires_in
            .as_u64()
            .or_else(|| response.expires_in.as_str()?.parse::<u64>().ok())
            .unwrap_or(300);
        let lifetime = Duration::from_secs(expires.saturating_sub(60).max(1));
        let value = response.access_token;
        *token = Some(AccessToken {
            value: value.clone(),
            refresh_at: Instant::now() + lifetime,
        });
        Ok(value)
    }

    async fn gateway_url(&self) -> Result<String, QQRuntimeError> {
        let token = self.access_token().await?;
        let response = self
            .http
            .get(format!("{QQ_API_BASE}/gateway/bot"))
            .header("Authorization", format!("QQBot {token}"))
            .header("X-Union-Appid", &self.credentials.app_id)
            .send()
            .await?;
        let status = response.status();
        let body = response.bytes().await?;
        if !status.is_success() {
            return Err(QQApiError::from_response(status, &body).into());
        }
        let value: Value = serde_json::from_slice(&body).map_err(|_| {
            QQRuntimeError::Protocol("gateway response is not valid JSON".to_owned())
        })?;
        value
            .get("url")
            .and_then(Value::as_str)
            .filter(|url| !url.is_empty())
            .map(ToOwned::to_owned)
            .ok_or_else(|| QQRuntimeError::Protocol("gateway response missing url".to_owned()))
    }

    async fn post_c2c_message(
        &self,
        openid: &str,
        content: &str,
        message_id: Option<&str>,
    ) -> Result<(), QQApiError> {
        let token = self.access_token().await.map_err(|error| QQApiError {
            code: None,
            status: None,
            message: safe_detail(&error.to_string()),
        })?;
        let mut payload = json!({"msg_type": 0, "content": content});
        if let (Some(payload), Some(message_id)) = (payload.as_object_mut(), message_id) {
            payload.insert("msg_id".to_owned(), Value::String(message_id.to_owned()));
        }
        let response = self
            .http
            .post(format!("{QQ_API_BASE}/v2/users/{openid}/messages"))
            .header("Authorization", format!("QQBot {token}"))
            .header("X-Union-Appid", &self.credentials.app_id)
            .json(&payload)
            .send()
            .await
            .map_err(|error| QQApiError {
                code: None,
                status: None,
                message: safe_detail(&error.to_string()),
            })?;
        let status = response.status();
        let body = response.bytes().await.map_err(|error| QQApiError {
            code: None,
            status: Some(status.as_u16()),
            message: safe_detail(&error.to_string()),
        })?;
        if status.is_success() {
            Ok(())
        } else {
            Err(QQApiError::from_response(status, &body))
        }
    }
}

impl QQApiError {
    fn from_response(status: StatusCode, body: &[u8]) -> Self {
        let value = serde_json::from_slice::<Value>(body).unwrap_or(Value::Null);
        let code = value
            .get("code")
            .and_then(|value| value.as_i64().or_else(|| value.as_str()?.parse().ok()));
        let message = value
            .get("message")
            .or_else(|| value.get("msg"))
            .and_then(Value::as_str)
            .map(safe_detail)
            .unwrap_or_else(|| safe_detail(&String::from_utf8_lossy(body)));
        Self {
            code,
            status: Some(status.as_u16()),
            message,
        }
    }
}

fn safe_detail(value: &str) -> String {
    redact_secrets(value)
        .replace(['\r', '\n'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(300)
        .collect()
}

struct RuntimeState {
    stop: AtomicBool,
    ready: AtomicBool,
    stop_notify: Notify,
    ready_notify: Notify,
}

impl RuntimeState {
    fn new() -> Self {
        Self {
            stop: AtomicBool::new(false),
            ready: AtomicBool::new(false),
            stop_notify: Notify::new(),
            ready_notify: Notify::new(),
        }
    }

    fn request_stop(&self) {
        self.stop.store(true, Ordering::Release);
        self.stop_notify.notify_waiters();
        // Preserve one permit for a waiter created immediately after the
        // broadcast. This closes the check/register race during shutdown.
        self.stop_notify.notify_one();
        self.ready_notify.notify_waiters();
    }
}

#[derive(Clone)]
pub struct QQRuntime {
    store: Arc<Store>,
    logger: ProcessSafeLogger,
    commands: CommandService,
    api: QQApiClient,
    limiter: RateLimiter,
    options: QQRuntimeOptions,
    state: Arc<RuntimeState>,
}

impl fmt::Debug for QQRuntime {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("QQRuntime")
            .field("store", &self.store.path)
            .field("options", &self.options)
            .finish_non_exhaustive()
    }
}

impl QQRuntime {
    pub fn new(
        store: Arc<Store>,
        credentials: Credentials,
        logger: ProcessSafeLogger,
        options: QQRuntimeOptions,
    ) -> Result<Self, QQRuntimeError> {
        Ok(Self {
            commands: CommandService::new(store.clone()),
            store,
            logger,
            api: QQApiClient::new(credentials)?,
            limiter: RateLimiter::default(),
            options,
            state: Arc::new(RuntimeState::new()),
        })
    }

    pub fn request_stop(&self) {
        self.state.request_stop();
    }

    pub fn is_stopping(&self) -> bool {
        self.state.stop.load(Ordering::Acquire)
    }

    pub fn is_ready(&self) -> bool {
        self.state.ready.load(Ordering::Acquire)
    }

    fn log_info(&self, message: &str) {
        let _ = self.logger.info(message);
    }

    fn log_warning(&self, message: &str) {
        let _ = self.logger.warning(message);
    }

    fn log_error(&self, message: &str) {
        let _ = self.logger.error(message);
    }

    async fn interruptible_sleep(&self, duration: Duration) {
        let notified = self.state.stop_notify.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        if duration.is_zero() || self.is_stopping() {
            return;
        }
        tokio::select! {
            _ = tokio::time::sleep(duration) => {}
            _ = &mut notified => {}
        }
    }

    async fn monitor_hosts(self) -> Result<(), QQRuntimeError> {
        let started = Instant::now();
        let mut empty_checks = 0_u32;
        while !self.is_stopping() {
            let hosts = self.store.list_hosts()?;
            let dead: Vec<_> = hosts
                .iter()
                .filter(|host| !process_matches(host.pid, host.create_time))
                .cloned()
                .collect();
            if !dead.is_empty() {
                self.store.remove_hosts(&dead)?;
            }
            let alive_count = hosts.len().saturating_sub(dead.len());
            let pending_work = self.store.companion_work_pending()?;
            if self.options.standalone {
                self.interruptible_sleep(self.options.monitor_interval)
                    .await;
                continue;
            }
            if alive_count > 0
                || pending_work
                || (!self.is_ready() && started.elapsed() < self.options.hostless_startup_grace)
            {
                empty_checks = 0;
            } else {
                empty_checks += 1;
                if empty_checks >= self.options.empty_host_checks.max(1) {
                    self.log_info("No Codex host remains; stopping companion");
                    self.request_stop();
                    return Ok(());
                }
            }
            self.interruptible_sleep(self.options.monitor_interval)
                .await;
        }
        Ok(())
    }

    async fn wait_until_ready(&self) -> bool {
        loop {
            let notified = self.state.ready_notify.notified();
            tokio::pin!(notified);
            notified.as_mut().enable();
            if self.is_ready() || self.is_stopping() {
                break;
            }
            notified.await;
        }
        self.is_ready() && !self.is_stopping()
    }

    async fn delivery_loop(self) -> Result<(), QQRuntimeError> {
        if !self.wait_until_ready().await {
            return Ok(());
        }
        while !self.is_stopping() && self.is_ready() {
            let Some(openid) = self.store.get_bound_openid()? else {
                self.interruptible_sleep(Duration::from_secs(1)).await;
                continue;
            };
            let Some(item) = self.store.get_due_outbox()? else {
                self.interruptible_sleep(Duration::from_millis(500)).await;
                continue;
            };
            let api = self.api.clone();
            let outcome = deliver_item(
                &self.store,
                &item,
                &openid,
                move |target, text| {
                    let api = api.clone();
                    let target = target.to_owned();
                    let text = text.to_owned();
                    async move { api.post_c2c_message(&target, &text, None).await }
                },
                &self.limiter,
            )
            .await
            .map_err(|error| QQRuntimeError::Protocol(error.to_string()))?;
            match outcome {
                DeliveryOutcome::Retry => self.interruptible_sleep(Duration::from_secs(1)).await,
                DeliveryOutcome::FailedPermanent => self.log_warning(
                    "QQ rejected proactive message permanently; use /last for the final reply",
                ),
                _ => {}
            }
        }
        Ok(())
    }

    async fn drain_outbox(&self) {
        if !self.is_ready() {
            return;
        }
        let deadline = tokio::time::Instant::now() + self.options.shutdown_drain_timeout;
        let mut retry_wait = false;
        loop {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                self.log_warning("Shutdown outbox drain deadline reached");
                return;
            }
            let openid = match self.store.get_bound_openid() {
                Ok(Some(openid)) => openid,
                Ok(None) => return,
                Err(error) => {
                    self.log_error(&format!("Shutdown outbox drain failed: {error}"));
                    return;
                }
            };
            let item = match self.store.get_due_outbox() {
                Ok(Some(item)) => item,
                Ok(None) if retry_wait => {
                    tokio::time::sleep(remaining.min(Duration::from_secs(1))).await;
                    continue;
                }
                Ok(None) => return,
                Err(error) => {
                    self.log_error(&format!("Shutdown outbox drain failed: {error}"));
                    return;
                }
            };
            let api = self.api.clone();
            let outcome = tokio::time::timeout(
                remaining,
                deliver_item(
                    &self.store,
                    &item,
                    &openid,
                    move |target, text| {
                        let api = api.clone();
                        let target = target.to_owned();
                        let text = text.to_owned();
                        async move { api.post_c2c_message(&target, &text, None).await }
                    },
                    &self.limiter,
                ),
            )
            .await;
            match outcome {
                Ok(Ok(outcome)) => retry_wait = outcome == DeliveryOutcome::Retry,
                Ok(Err(error)) => {
                    self.log_error(&format!(
                        "Shutdown outbox drain failed: {}",
                        safe_detail(&error.to_string())
                    ));
                    return;
                }
                Err(_) => {
                    self.log_warning("Shutdown outbox drain timed out");
                    return;
                }
            }
        }
    }

    async fn handle_dispatch(&self, event: Value) {
        let event_name = event.get("t").and_then(Value::as_str).unwrap_or_default();
        if !event_name.eq_ignore_ascii_case("C2C_MESSAGE_CREATE") {
            return;
        }
        let Some(data) = event.get("d") else {
            return;
        };
        let Some(openid) = data
            .get("author")
            .and_then(|value| value.get("user_openid"))
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
        else {
            return;
        };
        let Some(message_id) = data
            .get("id")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
        else {
            return;
        };
        let content = data
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let passive_api = self.api.clone();
        let active_api = self.api.clone();
        match self
            .commands
            .handle(
                &openid,
                &message_id,
                &content,
                move |target, text, source_id, _sequence| {
                    let api = passive_api.clone();
                    async move { api.post_c2c_message(&target, &text, Some(&source_id)).await }
                },
                move |target, text| {
                    let api = active_api.clone();
                    async move { api.post_c2c_message(&target, &text, None).await }
                },
            )
            .await
        {
            Ok(outcome) => self.log_info(&format!("Handled QQ command: {}", outcome.as_str())),
            Err(error) => self.log_error(&format!(
                "QQ command failed: {}",
                safe_detail(&error.to_string())
            )),
        }
    }

    async fn gateway_session(&self) -> Result<(), QQRuntimeError> {
        let gateway = self.api.gateway_url().await?;
        let (socket, _) = tokio_tungstenite::connect_async(gateway).await?;
        let (mut sink, mut stream) = socket.split();
        let hello = stream
            .next()
            .await
            .ok_or_else(|| QQRuntimeError::Protocol("gateway closed before hello".to_owned()))??;
        let hello = message_json(hello)?;
        if hello.get("op").and_then(Value::as_i64) != Some(10) {
            return Err(QQRuntimeError::Protocol(
                "gateway did not send Hello".to_owned(),
            ));
        }
        let heartbeat_ms = hello
            .get("d")
            .and_then(|value| value.get("heartbeat_interval"))
            .and_then(Value::as_u64)
            .unwrap_or(30_000)
            .max(1_000);
        let token = self.api.access_token().await?;
        sink.send(Message::Text(
            json!({
                "op": 2,
                "d": {
                    "token": format!("QQBot {token}"),
                    "intents": QQ_C2C_INTENT,
                    "shard": [0, 1]
                }
            })
            .to_string()
            .into(),
        ))
        .await?;

        let mut heartbeat = tokio::time::interval(Duration::from_millis(heartbeat_ms));
        heartbeat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut sequence: Option<i64> = None;
        loop {
            if self.is_stopping() {
                let _ = sink.send(Message::Close(None)).await;
                return Ok(());
            }
            tokio::select! {
                _ = self.state.stop_notify.notified() => {
                    let _ = sink.send(Message::Close(None)).await;
                    return Ok(());
                }
                _ = heartbeat.tick() => {
                    sink.send(Message::Text(json!({"op": 1, "d": sequence}).to_string().into())).await?;
                }
                incoming = stream.next() => {
                    let Some(incoming) = incoming else {
                        return Err(QQRuntimeError::Protocol("gateway disconnected".to_owned()));
                    };
                    let message = incoming?;
                    if message.is_close() {
                        return Err(QQRuntimeError::Protocol("gateway closed".to_owned()));
                    }
                    if message.is_ping() {
                        sink.send(Message::Pong(message.into_data())).await?;
                        continue;
                    }
                    if !message.is_text() && !message.is_binary() {
                        continue;
                    }
                    let event = message_json(message)?;
                    if let Some(value) = event.get("s").and_then(Value::as_i64) {
                        sequence = Some(value);
                    }
                    match event.get("op").and_then(Value::as_i64) {
                        Some(0) => {
                            if event.get("t").and_then(Value::as_str).is_some_and(|name| name.eq_ignore_ascii_case("READY")) {
                                if !self.state.ready.swap(true, Ordering::AcqRel) {
                                    self.log_info("QQ sandbox client is ready");
                                }
                                self.state.ready_notify.notify_waiters();
                            }
                            let runtime = self.clone();
                            tokio::spawn(async move { runtime.handle_dispatch(event).await; });
                        }
                        Some(1) => {
                            sink.send(Message::Text(json!({"op": 1, "d": sequence}).to_string().into())).await?;
                        }
                        Some(7 | 9) => return Err(QQRuntimeError::Protocol("gateway requested reconnect".to_owned())),
                        Some(11) => {}
                        _ => {}
                    }
                }
            }
        }
    }

    pub async fn run(&self) -> Result<(), QQRuntimeError> {
        let mut monitor = tokio::spawn(self.clone().monitor_hosts());
        let mut monitor_completed = false;
        let mut reconnect_delay = self.options.initial_reconnect_delay;
        let result = loop {
            if self.is_stopping() {
                break Ok(());
            }
            self.state.ready.store(false, Ordering::Release);
            let mut gateway: JoinHandle<Result<(), QQRuntimeError>> = {
                let runtime = self.clone();
                tokio::spawn(async move { runtime.gateway_session().await })
            };
            let mut delivery: JoinHandle<Result<(), QQRuntimeError>> = {
                let runtime = self.clone();
                tokio::spawn(async move { runtime.delivery_loop().await })
            };

            let stop_notified = self.state.stop_notify.notified();
            tokio::pin!(stop_notified);
            stop_notified.as_mut().enable();
            let connection_result = if self.is_stopping() {
                Ok(())
            } else {
                tokio::select! {
                    _ = &mut stop_notified => Ok(()),
                    result = &mut monitor => {
                        monitor_completed = true;
                        let result = flatten_join("Codex host monitor", result);
                        if let Err(error) = &result {
                            self.log_error(&format!("Codex host monitor failed: {}", safe_detail(&error.to_string())));
                        } else if !self.is_stopping() {
                            self.log_warning("Codex host monitor exited unexpectedly");
                        }
                        self.request_stop();
                        result
                    },
                    result = &mut gateway => flatten_join("QQ connection task", result),
                    result = &mut delivery => {
                        let result = flatten_join("QQ delivery loop", result);
                        if let Err(error) = &result {
                            self.log_error(&format!("QQ delivery loop failed: {}", safe_detail(&error.to_string())));
                        } else if !self.is_stopping() {
                            self.log_warning("QQ delivery loop stopped; reconnecting");
                        }
                        result
                    }
                }
            };
            gateway.abort();
            delivery.abort();
            let _ = gateway.await;
            let _ = delivery.await;

            if self.is_stopping() {
                break Ok(());
            }
            if let Err(error) = connection_result {
                self.log_error(&format!(
                    "QQ connection task failed: {}",
                    safe_detail(&error.to_string())
                ));
            }
            if self.is_ready() {
                reconnect_delay = self.options.initial_reconnect_delay;
            }
            self.state.ready.store(false, Ordering::Release);
            self.interruptible_sleep(reconnect_delay).await;
            reconnect_delay = (reconnect_delay * 2).min(Duration::from_secs(60));
        };

        self.request_stop();
        self.state.ready.store(true, Ordering::Release);
        self.drain_outbox().await;
        if !monitor_completed {
            if !monitor.is_finished() {
                monitor.abort();
            }
            let _ = monitor.await;
        }
        self.commands.shutdown().await;
        result
    }
}

fn flatten_join(
    label: &str,
    result: Result<Result<(), QQRuntimeError>, tokio::task::JoinError>,
) -> Result<(), QQRuntimeError> {
    result.unwrap_or_else(|error| {
        Err(QQRuntimeError::Protocol(format!(
            "{label} task failed: {}",
            safe_detail(&error.to_string())
        )))
    })
}

fn message_json(message: Message) -> Result<Value, QQRuntimeError> {
    let bytes = match message {
        Message::Text(text) => text.as_bytes().to_vec(),
        Message::Binary(bytes) => bytes.to_vec(),
        _ => {
            return Err(QQRuntimeError::Protocol(
                "gateway message is not JSON text".to_owned(),
            ));
        }
    };
    serde_json::from_slice(&bytes)
        .map_err(|_| QQRuntimeError::Protocol("gateway message is invalid JSON".to_owned()))
}

pub async fn run_qq_runtime(
    store: Arc<Store>,
    credentials: Credentials,
    logger: ProcessSafeLogger,
    standalone: bool,
) -> Result<(), QQRuntimeError> {
    let options = QQRuntimeOptions {
        standalone,
        ..QQRuntimeOptions::default()
    };
    run_qq_runtime_with_options(store, credentials, logger, options).await
}

pub async fn run_qq_runtime_with_options(
    store: Arc<Store>,
    credentials: Credentials,
    logger: ProcessSafeLogger,
    options: QQRuntimeOptions,
) -> Result<(), QQRuntimeError> {
    QQRuntime::new(store, credentials, logger, options)?
        .run()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sandbox_protocol_constants_match_the_c2c_contract() {
        assert_eq!(QQ_API_BASE, "https://sandbox.api.sgroup.qq.com");
        assert_eq!(QQ_C2C_INTENT, 1 << 25);
    }

    #[test]
    fn gateway_text_is_decoded_as_json() {
        let value = message_json(Message::Text(r#"{"op":10,"d":{}}"#.into())).unwrap();
        assert_eq!(value["op"], 10);
    }

    #[tokio::test]
    async fn a_stop_requested_before_waiting_keeps_a_notification_permit() {
        let state = RuntimeState::new();
        state.request_stop();
        tokio::time::timeout(Duration::from_millis(50), state.stop_notify.notified())
            .await
            .expect("stop notification must not be lost");
        assert!(state.stop.load(Ordering::Acquire));
    }
}
