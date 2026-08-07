//! Strict QQ command allow-list.

use chrono::{Local, TimeZone};
use regex::Regex;
use serde_json::Value;
use std::error::Error as StdError;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, OnceLock};
use std::time::Duration;
use thiserror::Error;

use crate::account_switch::{self, AccountSnapshot};
use crate::codex_accounts::CodexAccountManager;
use crate::codex_login::{AccountInfo, AppServerError, CodexAppServerClient};
use crate::codex_usage::{format_usage_text, parse_rate_limits, usage_dashboard_hint};
use crate::formatting::split_text;
use crate::security::redact_secrets;
use crate::store::{Store, StoreError};

pub const HELP_TEXT: &str = "CodexBot QQ 命令\n\
/bind 配对码 - 首次绑定或使用新配对码换绑\n\
/status - 查看 Codex 当前状态\n\
/last [项目] [页码] - 查看最近回复；只写页码时保持兼容\n\
/usage - 查看 Codex 各限额剩余百分比和重置时间\n\
/account - 查看当前 Codex 账号\n\
/account save 名称 - 保存当前 Codex 账号\n\
/account list - 列出 codex_login 账号和加密快照\n\
/account use 序号/名称/邮箱/ID - 安全切换账号\n\
/account delete 名称 - 删除已保存的账号\n\
/mute - 暂停主动通知\n\
/unmute - 恢复主动通知\n\
/help - 显示此帮助";

type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

pub trait CodexClient: Send + Sync {
    fn read_account(&self) -> BoxFuture<'_, Result<AccountInfo, AppServerError>>;
    fn read_rate_limits(&self) -> BoxFuture<'_, Result<Value, AppServerError>>;
}

impl CodexClient for CodexAppServerClient {
    fn read_account(&self) -> BoxFuture<'_, Result<AccountInfo, AppServerError>> {
        Box::pin(CodexAppServerClient::read_account(self))
    }

    fn read_rate_limits(&self) -> BoxFuture<'_, Result<Value, AppServerError>> {
        Box::pin(CodexAppServerClient::read_rate_limits(self))
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandOutcome {
    Duplicate,
    BadPairing,
    Bound,
    Unbound,
    Unauthorized,
    Replied,
}

impl CommandOutcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Duplicate => "duplicate",
            Self::BadPairing => "bad_pairing",
            Self::Bound => "bound",
            Self::Unbound => "unbound",
            Self::Unauthorized => "unauthorized",
            Self::Replied => "replied",
        }
    }
}

#[derive(Debug, Error)]
pub enum CommandError {
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error("QQ passive reply failed: {0}")]
    PassiveSend(String),
    #[error("background account operation failed: {0}")]
    Background(String),
}

#[derive(Clone)]
pub struct CommandService {
    store: Arc<Store>,
    codex_client: Arc<dyn CodexClient>,
    account_manager: Arc<CodexAccountManager>,
    codex_timeout: Duration,
}

impl std::fmt::Debug for CommandService {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CommandService")
            .field("store", &self.store.path)
            .field("account_manager", &self.account_manager)
            .field("codex_timeout", &self.codex_timeout)
            .finish_non_exhaustive()
    }
}

impl CommandService {
    pub fn new(store: Arc<Store>) -> Self {
        Self {
            store,
            codex_client: Arc::new(CodexAppServerClient::default()),
            account_manager: Arc::new(CodexAccountManager::new()),
            codex_timeout: Duration::from_secs(30),
        }
    }

    pub fn with_clients(
        store: Arc<Store>,
        codex_client: Arc<dyn CodexClient>,
        account_manager: Arc<CodexAccountManager>,
        codex_timeout: Duration,
    ) -> Self {
        Self {
            store,
            codex_client,
            account_manager,
            codex_timeout: codex_timeout.max(Duration::from_millis(100)),
        }
    }

    async fn usage_text(&self) -> String {
        let account = match tokio::time::timeout(
            self.codex_timeout,
            self.codex_client.read_account(),
        )
        .await
        {
            Ok(Ok(account)) => account,
            Ok(Err(error)) => return codex_failure_text("用量读取", &error, true),
            Err(_) => {
                return codex_failure_text(
                    "用量读取",
                    &AppServerError::Timeout("timeout".to_owned()),
                    true,
                );
            }
        };
        if !account.is_authenticated() || is_api_key_account(&account) {
            return format!(
                "Codex 当前未登录或使用 API key，app-server 无法读取限额。\n{}",
                usage_dashboard_hint()
            );
        }
        match tokio::time::timeout(self.codex_timeout, self.codex_client.read_rate_limits()).await {
            Ok(Ok(payload)) => format_usage_text(&parse_rate_limits(&payload)),
            Ok(Err(error)) => codex_failure_text("用量读取", &error, true),
            Err(_) => codex_failure_text(
                "用量读取",
                &AppServerError::Timeout("timeout".to_owned()),
                true,
            ),
        }
    }

    async fn account_text(&self) -> String {
        match tokio::time::timeout(self.codex_timeout, self.codex_client.read_account()).await {
            Ok(Ok(account)) => account_text(&account),
            Ok(Err(error)) => codex_failure_text("账号读取", &error, false),
            Err(_) => codex_failure_text(
                "账号读取",
                &AppServerError::Timeout("timeout".to_owned()),
                false,
            ),
        }
    }

    pub async fn shutdown(&self) {}

    pub async fn handle<FP, PFut, PA, AFut, PE, AE>(
        &self,
        openid: &str,
        message_id: &str,
        content: &str,
        mut passive_send: FP,
        mut active_send: PA,
    ) -> Result<CommandOutcome, CommandError>
    where
        FP: FnMut(String, String, String, u32) -> PFut,
        PFut: Future<Output = Result<(), PE>>,
        PA: FnMut(String, String) -> AFut,
        AFut: Future<Output = Result<(), AE>>,
        PE: StdError,
        AE: StdError,
    {
        if !self.store.remember_inbound(message_id)? {
            return Ok(CommandOutcome::Duplicate);
        }
        let command = content.split_whitespace().collect::<Vec<_>>().join(" ");
        if let Some(code) = bind_regex()
            .captures(&command)
            .and_then(|captures| captures.get(1))
            .map(|value| value.as_str())
        {
            if !self.store.consume_pairing(code, openid)? {
                send_passive(
                    &mut passive_send,
                    openid,
                    "配对码无效或已过期，请在源码目录运行 .\\codexbot.cmd pair。",
                    message_id,
                )
                .await?;
                return Ok(CommandOutcome::BadPairing);
            }
            let active_ok =
                active_send(openid.to_owned(), "CodexBot 主动通知测试成功。".to_owned())
                    .await
                    .is_ok();
            let response = if active_ok {
                "绑定成功，主动通知能力正常。"
            } else {
                "绑定已完成，但主动通知测试失败。请在 QQ 中开启“允许主动发送”，再用 /status 检查。"
            };
            send_passive(&mut passive_send, openid, response, message_id).await?;
            return Ok(CommandOutcome::Bound);
        }

        let Some(bound) = self.store.get_bound_openid()? else {
            send_passive(
                &mut passive_send,
                openid,
                "机器人尚未绑定。请在源码目录运行 .\\codexbot.cmd pair 后发送 /bind 配对码。",
                message_id,
            )
            .await?;
            return Ok(CommandOutcome::Unbound);
        };
        if !hmac_equal(&bound, openid) {
            return Ok(CommandOutcome::Unauthorized);
        }

        let lower = command.to_lowercase();
        let response = if lower == "/status" {
            status_text(&self.store)?
        } else if lower == "/usage" {
            self.usage_text().await
        } else if lower == "/account" {
            self.account_text().await
        } else if lower.starts_with("/account ") {
            self.account_switch_text(command["/account".len()..].trim())
                .await
        } else if lower == "/last" || lower.starts_with("/last ") {
            match last_arguments(command.get(5..).unwrap_or_default()) {
                Some((project, page)) => last_text(&self.store, page, project.as_deref())?,
                None => "用法：/last、/last 页码、/last 项目 [页码]".to_owned(),
            }
        } else if lower == "/mute" {
            self.store.set_muted(true)?;
            "主动通知已暂停；状态和最近回复仍会更新，不会补发静音期间的旧通知。".to_owned()
        } else if lower == "/unmute" {
            self.store.set_muted(false)?;
            "主动通知已恢复，只推送之后的新事件。".to_owned()
        } else if lower == "/help" {
            HELP_TEXT.to_owned()
        } else {
            format!("未知命令。\n\n{HELP_TEXT}")
        };
        send_passive(&mut passive_send, openid, &response, message_id).await?;
        Ok(CommandOutcome::Replied)
    }

    async fn account_switch_text(&self, argument: &str) -> String {
        let tokens: Vec<_> = argument.split_whitespace().collect();
        let action = tokens
            .first()
            .map(|value| value.to_lowercase())
            .unwrap_or_default();
        match action.as_str() {
            "list" => self.account_sources_text().await,
            "save" => {
                if tokens.len() < 2 {
                    return "用法：/account save 名称".to_owned();
                }
                let name = tokens[1..].join(" ");
                match spawn_blocking_account(move || account_switch::save_current_account(&name)).await {
                    Ok(snapshot) => format!(
                        "已保存当前账号为 {}（{}）。\n用 /account use {} 可随时切换。",
                        safe_field(Some(&snapshot.name), "未知", 160),
                        safe_field(snapshot.email.as_deref(), "未识别邮箱", 160),
                        safe_field(Some(&snapshot.name), "未知", 160)
                    ),
                    Err(error) => account_switch_error("保存", &error),
                }
            }
            "use" => {
                if tokens.len() < 2 {
                    return "用法：/account use 序号、名称、邮箱或账号 ID".to_owned();
                }
                self.use_account(tokens[1..].join(" ")).await
            }
            "delete" => {
                if tokens.len() < 2 {
                    return "用法：/account delete 名称".to_owned();
                }
                let name = tokens[1..].join(" ");
                let operation_name = name.clone();
                match spawn_blocking_account(move || account_switch::delete_account(&operation_name)).await {
                    Ok(()) => format!("已删除账号 {}。", safe_field(Some(&name), "未知", 160)),
                    Err(error) => account_switch_error("删除", &error),
                }
            }
            _ => "用法：\n/account save 名称 - 保存当前账号\n/account list - 列出 codex_login 账号和加密快照\n/account use 序号/名称/邮箱/ID - 安全切换账号\n/account delete 名称 - 删除 CodexBot 加密快照".to_owned(),
        }
    }

    async fn account_sources_text(&self) -> String {
        let manager = self.account_manager.clone();
        let external = match spawn_blocking_account(move || manager.load_store()).await {
            Ok(store) => store,
            Err(error) => return account_switch_error("读取 codex_login 账号", &error),
        };
        let snapshots = match spawn_blocking_account(account_switch::list_accounts).await {
            Ok(snapshots) => snapshots,
            Err(error) => return account_switch_error("读取 CodexBot 加密快照", &error),
        };
        let mut lines = Vec::new();
        if !external.accounts.is_empty() {
            lines.push("codex_login 已保存账号：".to_owned());
            for (index, account) in external.accounts.iter().enumerate() {
                let mut markers = Vec::new();
                if external.active_account_id.as_deref() == Some(&account.id) {
                    markers.push("当前");
                }
                if !account.is_ready() {
                    markers.push("登录过期");
                }
                let suffix = if markers.is_empty() {
                    String::new()
                } else {
                    format!("（{}）", markers.join("、"))
                };
                lines.push(format!(
                    "{}. {} · {}{}",
                    index + 1,
                    safe_field(Some(&account.name), "未知", 160),
                    safe_field(account.email.as_deref(), "无邮箱", 160),
                    suffix
                ));
            }
        }
        append_snapshots(&mut lines, &snapshots);
        if lines.is_empty() {
            return "还没有可切换的账号。\n可先在 codex_login 中添加账号，或用 /account save 名称保存当前登录。".to_owned();
        }
        lines.push(String::new());
        lines.push("切换：/account use 序号、名称、邮箱或账号 ID".to_owned());
        lines.join("\n")
    }

    async fn use_account(&self, selector: String) -> String {
        let manager = self.account_manager.clone();
        let external = match spawn_blocking_account(move || manager.load_store()).await {
            Ok(store) => store,
            Err(error) => return account_switch_error("读取 codex_login 账号", &error),
        };
        let mut external_error: Option<String> = None;
        if !external.accounts.is_empty() {
            let manager = self.account_manager.clone();
            let candidate = selector.clone();
            match spawn_blocking_account(move || manager.resolve_account(&candidate)).await {
                Ok(_) => {
                    let manager = self.account_manager.clone();
                    let candidate = selector.clone();
                    return match spawn_blocking_account(move || manager.switch_account(&candidate))
                        .await
                    {
                        Ok(account) => format!(
                            "已切换到 codex_login 账号 {}（{}）。\n已更新 Codex 登录并同步当前账号；现在可以重新启动 Codex。",
                            safe_field(Some(&account.name), "未知", 160),
                            safe_field(account.email.as_deref(), "无邮箱", 160)
                        ),
                        Err(error) => account_switch_error("切换 codex_login 账号", &error),
                    };
                }
                Err(error) => external_error = Some(error),
            }
        }

        let snapshots = match spawn_blocking_account(account_switch::list_accounts).await {
            Ok(snapshots) => snapshots,
            Err(error) => return account_switch_error("读取 CodexBot 加密快照", &error),
        };
        let local = snapshots
            .iter()
            .any(|item| item.name.eq_ignore_ascii_case(&selector));
        if !local {
            if let Some(error) = external_error {
                return account_switch_error("切换 codex_login 账号", &error);
            }
        }
        let candidate = selector.clone();
        match spawn_blocking_account(move || account_switch::switch_account(&candidate)).await {
            Ok((email, _)) => format!(
                "已切换到 CodexBot 加密快照 {}（{}）。\n已更新 Codex 登录；现在可以重新启动 Codex。",
                safe_field(Some(&selector), "未知", 160),
                safe_field(email.as_deref(), "未知邮箱", 160)
            ),
            Err(error) => account_switch_error("切换 CodexBot 加密快照", &error),
        }
    }
}

async fn send_passive<F, Fut, E>(
    sender: &mut F,
    openid: &str,
    text: &str,
    message_id: &str,
) -> Result<(), CommandError>
where
    F: FnMut(String, String, String, u32) -> Fut,
    Fut: Future<Output = Result<(), E>>,
    E: StdError,
{
    sender(openid.to_owned(), text.to_owned(), message_id.to_owned(), 1)
        .await
        .map_err(|error| {
            CommandError::PassiveSend(safe_field(Some(&error.to_string()), "unknown", 240))
        })
}

async fn spawn_blocking_account<F, T, E>(operation: F) -> Result<T, String>
where
    F: FnOnce() -> Result<T, E> + Send + 'static,
    T: Send + 'static,
    E: std::fmt::Display + Send + 'static,
{
    tokio::task::spawn_blocking(operation)
        .await
        .map_err(|error| error.to_string())?
        .map_err(|error| error.to_string())
}

fn append_snapshots(lines: &mut Vec<String>, snapshots: &[AccountSnapshot]) {
    if snapshots.is_empty() {
        return;
    }
    if !lines.is_empty() {
        lines.push(String::new());
    }
    lines.push("CodexBot 加密快照：".to_owned());
    for snapshot in snapshots {
        lines.push(format!(
            "• {}（{}）",
            safe_field(Some(&snapshot.name), "未知", 160),
            safe_field(snapshot.email.as_deref(), "未识别邮箱", 160)
        ));
    }
}

fn bind_regex() -> &'static Regex {
    static REGEX: OnceLock<Regex> = OnceLock::new();
    REGEX.get_or_init(|| Regex::new(r"(?i)^/bind\s+([A-Za-z0-9-]+)$").expect("valid bind regex"))
}

fn safe_field(value: Option<&str>, fallback: &str, limit: usize) -> String {
    let value = redact_secrets(value.unwrap_or_default())
        .replace(['\r', '\n'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let value: String = value.chars().take(limit).collect();
    if value.is_empty() {
        fallback.to_owned()
    } else {
        value
    }
}

fn auth_type_text(value: &str) -> String {
    match value.to_lowercase().replace('-', "_").as_str() {
        "apikey" | "api_key" | "api key" => "API key".to_owned(),
        "chatgpt" | "chatgpt_login" | "openai" | "openai_auth" => "ChatGPT 登录".to_owned(),
        "not_logged_in" => "未登录".to_owned(),
        _ => safe_field(Some(value), "未知", 160),
    }
}

fn account_text(account: &AccountInfo) -> String {
    if !account.is_authenticated() {
        return "Codex 当前未登录（或当前认证方式不支持 OpenAI 账号读取）。\n认证类型：未登录\n请在 Codex 中登录后重试 /account。".to_owned();
    }
    format!(
        "Codex 当前账号\n邮箱：{}\n套餐：{}\n认证类型：{}",
        safe_field(account.email.as_deref(), "未知", 160),
        safe_field(account.plan.as_deref(), "未知", 160),
        auth_type_text(&account.auth_type)
    )
}

fn is_api_key_account(account: &AccountInfo) -> bool {
    matches!(
        account.auth_type.to_lowercase().replace('-', "_").as_str(),
        "apikey" | "api_key" | "api key"
    )
}

fn auth_required_error(error: &AppServerError) -> bool {
    error.rpc_code() == Some(-32600)
        || error
            .to_string()
            .to_lowercase()
            .contains("authentication required")
}

fn codex_failure_text(action: &str, error: &AppServerError, dashboard: bool) -> String {
    let text = if error.is_timeout() {
        format!("Codex {action}超时，请稍后重试。")
    } else if auth_required_error(error) {
        format!("Codex {action}需要 ChatGPT 账号认证；当前可能未登录或使用 API key。")
    } else if error.rpc_code() == Some(-32601) {
        format!(
            "当前 Codex 版本不支持 {action} 接口，请升级 Codex CLI（建议 0.146.0 或更新版本）。"
        )
    } else {
        format!("Codex {action}暂不可用，可能是旧版 app-server 或进程未运行。")
    };
    if dashboard {
        format!("{text}\n{}", usage_dashboard_hint())
    } else {
        text
    }
}

fn last_arguments(argument: &str) -> Option<(Option<String>, usize)> {
    let mut tokens: Vec<String> = argument.split_whitespace().map(ToOwned::to_owned).collect();
    if tokens
        .first()
        .is_some_and(|value| matches!(value.to_lowercase().as_str(), "--project" | "-p"))
    {
        tokens.remove(0);
        if tokens.is_empty() {
            return None;
        }
    }
    if tokens
        .first()
        .is_some_and(|value| value.to_lowercase().starts_with("--project="))
    {
        tokens[0] = tokens[0]
            .split_once('=')
            .map(|(_, value)| value)
            .unwrap_or_default()
            .to_owned();
    }
    let parsed_page = tokens
        .last()
        .filter(|value| value.chars().all(|character| character.is_ascii_digit()))
        .and_then(|value| value.parse::<usize>().ok());
    if parsed_page.is_some() {
        tokens.pop();
    }
    let page = parsed_page.unwrap_or(1);
    if page < 1 {
        return None;
    }
    let project = (!tokens.is_empty()).then(|| tokens.join(" "));
    Some((project, page))
}

fn last_text(store: &Store, page: usize, project: Option<&str>) -> Result<String, StoreError> {
    let Some(reply) = store.get_last_reply(project, None)? else {
        if let Some(project) = project {
            let available = store.get_last_reply_projects()?;
            let suffix = if available.is_empty() {
                "当前没有可用项目".to_owned()
            } else {
                format!("可用项目：{}", available.join("、"))
            };
            return Ok(format!(
                "找不到项目“{}”的 Codex 回复。{suffix}。",
                safe_field(Some(project), "未知", 160)
            ));
        }
        return Ok("还没有可读取的 Codex 最终回复。".to_owned());
    };
    let chunks = split_text(&reply.content, 1000)
        .map_err(|error| StoreError::InvalidOutboxState(error.to_string()))?;
    if page < 1 || page > chunks.len() {
        return Ok(format!("页码无效，可用范围：1-{}。", chunks.len()));
    }
    let title = if project.is_none() {
        "最近一次".to_owned()
    } else {
        format!("项目 {} 最近一次", reply.project)
    };
    Ok(format!(
        "{title} Codex 回复 [{page}/{}]\n项目：{}\n模型：{}\n\n{}",
        chunks.len(),
        reply.project,
        reply.model,
        chunks[page - 1]
    ))
}

fn status_text(store: &Store) -> Result<String, StoreError> {
    let sessions = store.get_sessions_for_status()?;
    if sessions.is_empty() {
        return Ok("当前还没有收到 Codex 任务状态。".to_owned());
    }
    let mut lines = vec![format!(
        "CodexBot：{}",
        if store.is_muted()? {
            "已静音"
        } else {
            "通知开启"
        }
    )];
    for session in sessions {
        let seconds = session.updated_at.trunc() as i64;
        let nanos = (session.updated_at.fract().abs() * 1_000_000_000.0) as u32;
        let updated = Local
            .timestamp_opt(seconds, nanos)
            .single()
            .map(|value| value.format("%m-%d %H:%M:%S").to_string())
            .unwrap_or_else(|| "未知".to_owned());
        let status = match session.status.as_str() {
            "idle" => "空闲",
            "running" => "处理中",
            "awaiting_approval" => "等待本机审批",
            "completed" => "已完成",
            "closed" => "已关闭",
            value => value,
        };
        lines.extend([
            String::new(),
            format!("项目：{}", session.project),
            format!("模型：{}", session.model),
            format!("状态：{status}"),
            format!("更新：{updated}"),
        ]);
    }
    Ok(lines.join("\n"))
}

fn account_switch_error(prefix: &str, error: &str) -> String {
    let detail = safe_field(Some(error), "请稍后重试", 220);
    let mut response = format!("{prefix}失败：{detail}");
    if detail.contains("正在运行") {
        response.push_str(
            "\n请先运行 .\\codexbot.cmd start 保持机器人在线，再完全退出 Codex/ChatGPT 后重试。",
        );
    }
    response
}

pub fn hmac_equal(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    let mut difference = left.len() ^ right.len();
    for index in 0..left.len().max(right.len()) {
        difference |= usize::from(
            left.get(index).copied().unwrap_or(0) ^ right.get(index).copied().unwrap_or(0),
        );
    }
    difference == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_last_arguments_compatibly() {
        assert_eq!(last_arguments(""), Some((None, 1)));
        assert_eq!(last_arguments("2"), Some((None, 2)));
        assert_eq!(
            last_arguments("project name 3"),
            Some((Some("project name".to_owned()), 3))
        );
        assert_eq!(last_arguments("--project"), None);
    }

    #[test]
    fn constant_time_comparison_handles_different_lengths() {
        assert!(hmac_equal("owner", "owner"));
        assert!(!hmac_equal("owner", "other"));
    }
}
