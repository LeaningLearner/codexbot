//! Compatibility with accounts saved by Codex Switcher (`codex_login`).
//!
//! Credentials are deliberately kept in memory only.  Display helpers and
//! `Debug` implementations never expose token or API-key values.

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use rand::RngCore;
use serde_json::{Map, Value, json};
use std::collections::HashSet;
use std::ffi::OsStr;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};
use sysinfo::{ProcessesToUpdate, System};
use thiserror::Error;
use zeroize::{Zeroize, ZeroizeOnDrop};

use crate::security::redact_secrets;

pub const CODEX_SWITCHER_HOME_ENV: &str = "CODEX_SWITCHER_HOME";
pub const CODEX_SWITCHER_DIR_NAME: &str = ".codex-switcher";
pub const MAX_ACCOUNT_JSON_BYTES: u64 = 8 * 1024 * 1024;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{message}")]
pub struct CodexAccountError {
    message: String,
}

impl CodexAccountError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl From<io::Error> for CodexAccountError {
    fn from(error: io::Error) -> Self {
        Self::new(format!("Codex 账号文件操作失败：{}", error.kind()))
    }
}

#[derive(Clone, Zeroize, ZeroizeOnDrop)]
pub struct CodexTokens {
    pub id_token: String,
    pub access_token: String,
    pub refresh_token: String,
    pub account_id: Option<String>,
}

impl fmt::Debug for CodexTokens {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexTokens")
            .field("id_token", &"[REDACTED]")
            .field("access_token", &"[REDACTED]")
            .field("refresh_token", &"[REDACTED]")
            .field("account_id", &self.account_id)
            .finish()
    }
}

impl PartialEq for CodexTokens {
    fn eq(&self, other: &Self) -> bool {
        self.id_token == other.id_token
            && self.access_token == other.access_token
            && self.refresh_token == other.refresh_token
            && self.account_id == other.account_id
    }
}

#[derive(Clone, PartialEq)]
pub struct CodexAccount {
    pub id: String,
    pub name: String,
    pub email: Option<String>,
    pub plan: Option<String>,
    pub auth_type: String,
    pub auth_state: String,
    pub tokens: Option<CodexTokens>,
    pub api_key: Option<String>,
}

impl fmt::Debug for CodexAccount {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexAccount")
            .field("id", &self.id)
            .field("name", &self.name)
            .field("email", &self.email)
            .field("plan", &self.plan)
            .field("auth_type", &self.auth_type)
            .field("auth_state", &self.auth_state)
            .field("tokens", &self.tokens.as_ref().map(|_| "[REDACTED]"))
            .field("api_key", &self.api_key.as_ref().map(|_| "[REDACTED]"))
            .finish()
    }
}

impl CodexAccount {
    pub fn is_chatgpt(&self) -> bool {
        self.auth_type == "chatgpt" && self.tokens.is_some()
    }

    pub fn is_ready(&self) -> bool {
        self.auth_state != "reauth_required"
    }

    pub fn display_name(&self) -> &str {
        self.email
            .as_deref()
            .filter(|value| !value.is_empty())
            .or_else(|| (!self.name.is_empty()).then_some(self.name.as_str()))
            .unwrap_or(&self.id)
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct CodexAccountStore {
    pub accounts: Vec<CodexAccount>,
    pub active_account_id: Option<String>,
}

fn text(value: Option<&Value>) -> Option<String> {
    match value? {
        Value::Null => None,
        Value::String(value) => {
            let value = value.trim();
            (!value.is_empty()).then(|| value.to_owned())
        }
        value => {
            let value = value.to_string();
            let value = value.trim();
            (!value.is_empty()).then(|| value.to_owned())
        }
    }
}

fn first_text(raw: &Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter().find_map(|key| text(raw.get(*key)))
}

fn normalized(value: Option<&Value>) -> String {
    text(value)
        .unwrap_or_default()
        .to_lowercase()
        .chars()
        .filter(|character| character.is_ascii_alphanumeric())
        .collect()
}

fn normalize_auth_type(value: Option<&Value>) -> String {
    match normalized(value).as_str() {
        "chatgpt" | "chatgptlogin" | "openaiauth" | "oauth" => "chatgpt",
        "apikey" | "openaiapikey" => "api_key",
        _ => "unknown",
    }
    .to_owned()
}

fn normalize_auth_state(value: Option<&Value>) -> String {
    let value = value
        .map(Some)
        .map(normalized)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "ready".to_owned());
    if value == "ready" {
        "ready".to_owned()
    } else {
        "reauth_required".to_owned()
    }
}

fn parse_tokens(value: Option<&Value>) -> Option<CodexTokens> {
    let raw = value?.as_object()?;
    let id_token = first_text(raw, &["id_token", "idToken"])?;
    let access_token = first_text(raw, &["access_token", "accessToken"])?;
    let refresh_token = first_text(raw, &["refresh_token", "refreshToken"])?;
    Some(CodexTokens {
        id_token,
        access_token,
        refresh_token,
        account_id: first_text(raw, &["account_id", "accountId"]),
    })
}

fn parse_account(value: &Value, index: usize) -> Result<CodexAccount, CodexAccountError> {
    let empty = Map::new();
    let raw = value.as_object().unwrap_or(&empty);
    let id = text(raw.get("id"))
        .ok_or_else(|| CodexAccountError::new(format!("accounts.json 第 {index} 个账号缺少 id")))?;
    let auth_data = raw
        .get("auth_data")
        .or_else(|| raw.get("authData"))
        .and_then(Value::as_object)
        .unwrap_or(&empty);

    let mut auth_type = normalize_auth_type(
        raw.get("auth_mode")
            .or_else(|| raw.get("authMode"))
            .or_else(|| auth_data.get("type")),
    );
    let tokens = auth_data
        .get("tokens")
        .filter(|value| value.is_object())
        .and_then(|value| parse_tokens(Some(value)))
        .or_else(|| parse_tokens(Some(&Value::Object(auth_data.clone()))));
    let api_key = first_text(auth_data, &["key", "OPENAI_API_KEY", "openai_api_key"])
        .or_else(|| first_text(raw, &["OPENAI_API_KEY", "openai_api_key"]));
    if auth_type == "unknown" {
        if tokens.is_some() {
            auth_type = "chatgpt".to_owned();
        } else if api_key.is_some() {
            auth_type = "api_key".to_owned();
        }
    }

    let email = first_text(raw, &["email", "email_address", "emailAddress"]);
    let name = first_text(raw, &["name"])
        .or_else(|| email.clone())
        .unwrap_or_else(|| {
            let suffix = id
                .chars()
                .rev()
                .take(8)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>();
            format!("Codex 账号 {suffix}")
        });
    Ok(CodexAccount {
        id,
        name,
        email,
        plan: first_text(raw, &["plan_type", "planType", "plan"]),
        auth_type,
        auth_state: normalize_auth_state(raw.get("auth_state").or_else(|| raw.get("authState"))),
        tokens,
        api_key,
    })
}

fn store_from_root(root: &Map<String, Value>) -> Result<CodexAccountStore, CodexAccountError> {
    let accounts = match root.get("accounts") {
        None => &[][..],
        Some(Value::Array(accounts)) => accounts.as_slice(),
        Some(_) => {
            return Err(CodexAccountError::new(
                "Codex 账号文件中的 accounts 必须是数组",
            ));
        }
    };
    let accounts = accounts
        .iter()
        .enumerate()
        .map(|(index, value)| parse_account(value, index + 1))
        .collect::<Result<Vec<_>, _>>()?;
    let mut ids = HashSet::new();
    if accounts
        .iter()
        .any(|account| !ids.insert(account.id.clone()))
    {
        return Err(CodexAccountError::new("Codex 账号文件包含重复的账号 ID"));
    }
    let active_account_id = text(
        root.get("active_account_id")
            .or_else(|| root.get("activeAccountId")),
    );
    if active_account_id
        .as_ref()
        .is_some_and(|active| !ids.contains(active))
    {
        return Err(CodexAccountError::new("Codex 账号文件的当前账号 ID 不存在"));
    }
    Ok(CodexAccountStore {
        accounts,
        active_account_id,
    })
}

fn read_json_snapshot(path: &Path) -> Result<(Map<String, Value>, Vec<u8>), CodexAccountError> {
    let file = File::open(path).map_err(|error| {
        CodexAccountError::new(format!("无法读取 Codex 账号文件：{}", error.kind()))
    })?;
    let metadata = file.metadata().map_err(|error| {
        CodexAccountError::new(format!("无法读取 Codex 账号文件：{}", error.kind()))
    })?;
    if metadata.len() > MAX_ACCOUNT_JSON_BYTES {
        return Err(CodexAccountError::new(format!(
            "账号文件过大：{}",
            path.file_name()
                .and_then(OsStr::to_str)
                .unwrap_or("accounts.json")
        )));
    }
    let mut content = Vec::with_capacity(metadata.len() as usize);
    file.take(MAX_ACCOUNT_JSON_BYTES + 1)
        .read_to_end(&mut content)
        .map_err(|error| {
            CodexAccountError::new(format!("无法读取 Codex 账号文件：{}", error.kind()))
        })?;
    if content.len() as u64 > MAX_ACCOUNT_JSON_BYTES {
        return Err(CodexAccountError::new("账号文件过大：accounts.json"));
    }
    let value: Value = serde_json::from_slice(&content)
        .map_err(|_| CodexAccountError::new("Codex 账号文件不是有效 JSON"))?;
    let root = value
        .as_object()
        .cloned()
        .ok_or_else(|| CodexAccountError::new("Codex 账号文件根节点必须是对象"))?;
    Ok((root, content))
}

fn read_json(path: &Path) -> Result<Map<String, Value>, CodexAccountError> {
    read_json_snapshot(path).map(|(root, _)| root)
}

fn temporary_path(path: &Path) -> PathBuf {
    let mut random = [0_u8; 8];
    rand::thread_rng().fill_bytes(&mut random);
    let file_name = path
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or("auth.json");
    path.with_file_name(format!("{file_name}.{}.tmp", hex::encode(random)))
}

#[cfg(windows)]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: both buffers are valid, NUL-terminated UTF-16 strings for the
    // duration of the call.
    let result = unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    std::fs::rename(source, target)
}

fn restrict_permissions(_path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(_path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn atomic_write_bytes(path: &Path, value: &[u8]) -> Result<(), CodexAccountError> {
    let parent = path
        .parent()
        .ok_or_else(|| CodexAccountError::new("Codex 登录路径没有父目录"))?;
    std::fs::create_dir_all(parent).map_err(|error| {
        CodexAccountError::new(format!("无法恢复 Codex 登录文件：{}", error.kind()))
    })?;
    let temporary = temporary_path(path);
    let result = (|| -> io::Result<()> {
        let mut handle = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        handle.write_all(value)?;
        handle.sync_all()?;
        drop(handle);
        replace_file(&temporary, path)?;
        restrict_permissions(path)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result.map_err(|error| {
        CodexAccountError::new(format!("无法恢复 Codex 登录文件：{}", error.kind()))
    })
}

fn atomic_write_json(
    path: &Path,
    value: &Map<String, Value>,
    expected_content: Option<&[u8]>,
) -> Result<(), CodexAccountError> {
    if let Some(expected) = expected_content {
        let current = std::fs::read(path)
            .map_err(|_| CodexAccountError::new("__concurrent_store_change__"))?;
        if current != expected {
            return Err(CodexAccountError::new("__concurrent_store_change__"));
        }
    }
    let mut content = serde_json::to_vec_pretty(&Value::Object(value.clone()))
        .map_err(|_| CodexAccountError::new("无法序列化 Codex 账号文件"))?;
    content.push(b'\n');
    atomic_write_bytes(path, &content).map_err(|error| {
        if error.message == "__concurrent_store_change__" {
            error
        } else {
            CodexAccountError::new(format!("无法写入 Codex 账号文件：{error}"))
        }
    })
}

fn update_account_store<F>(path: &Path, mut mutate: F) -> Result<(), CodexAccountError>
where
    F: FnMut(&mut Map<String, Value>) -> Result<bool, CodexAccountError>,
{
    let lock_path = path.with_extension(format!(
        "{}lock",
        path.extension()
            .and_then(OsStr::to_str)
            .map(|suffix| format!("{suffix}."))
            .unwrap_or_default()
    ));
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(CodexAccountError::from)?;
    let deadline = Instant::now() + Duration::from_secs(2);
    while lock.try_lock_exclusive().is_err() {
        if Instant::now() >= deadline {
            return Err(CodexAccountError::new(
                "Codex 账号文件正被其他操作占用，请稍后重试",
            ));
        }
        thread::sleep(Duration::from_millis(20));
    }

    let result = (|| {
        for _ in 0..3 {
            let (mut root, original) = read_json_snapshot(path)?;
            if !mutate(&mut root)? {
                return Ok(());
            }
            match atomic_write_json(path, &root, Some(&original)) {
                Err(error) if error.message.contains("__concurrent_store_change__") => continue,
                result => return result,
            }
        }
        Err(CodexAccountError::new("Codex 账号文件持续变化，请稍后重试"))
    })();
    let _ = FileExt::unlock(&lock);
    result
}

fn jwt_claims(id_token: &str) -> (Option<String>, Option<String>, Option<String>) {
    let parts: Vec<_> = id_token.split('.').collect();
    if parts.len() != 3 {
        return (None, None, None);
    }
    let mut encoded = parts[1].to_owned();
    encoded.extend(std::iter::repeat_n('=', (4 - encoded.len() % 4) % 4));
    let Ok(decoded) = URL_SAFE.decode(encoded.as_bytes()) else {
        return (None, None, None);
    };
    let Ok(Value::Object(payload)) = serde_json::from_slice::<Value>(&decoded) else {
        return (None, None, None);
    };
    let auth = payload
        .get("https://api.openai.com/auth")
        .and_then(Value::as_object);
    (
        text(payload.get("email")),
        auth.and_then(|raw| text(raw.get("chatgpt_plan_type"))),
        auth.and_then(|raw| text(raw.get("chatgpt_account_id"))),
    )
}

fn tokens_from_auth(root: &Map<String, Value>) -> (Option<CodexTokens>, Option<String>) {
    (
        parse_tokens(root.get("tokens")),
        first_text(root, &["OPENAI_API_KEY", "openai_api_key"]),
    )
}

fn same_identity(account: &CodexAccount, tokens: &CodexTokens) -> bool {
    let (email, _, claim_id) = jwt_claims(&tokens.id_token);
    let current_id = tokens.account_id.as_ref().or(claim_id.as_ref());
    let stored_id = account.tokens.as_ref().and_then(|stored| {
        let (_, _, claim_id) = jwt_claims(&stored.id_token);
        stored.account_id.clone().or(claim_id)
    });
    if let (Some(stored), Some(current)) = (stored_id.as_ref(), current_id) {
        return stored == current;
    }
    matches!(
        (account.email.as_ref(), email.as_ref()),
        (Some(stored), Some(current)) if stored.eq_ignore_ascii_case(current)
    )
}

fn safe_display(value: Option<&str>, fallback: &str, limit: usize) -> String {
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

fn process_stem(value: &str) -> String {
    let name = value
        .replace('/', "\\")
        .rsplit('\\')
        .next()
        .unwrap_or_default()
        .to_lowercase();
    [".exe", ".cmd", ".bat", ".com", ".js"]
        .iter()
        .find_map(|suffix| name.strip_suffix(suffix).map(ToOwned::to_owned))
        .unwrap_or(name)
}

fn is_codex_package_chatgpt_path(value: &str) -> bool {
    let normalized = value
        .trim()
        .trim_matches('"')
        .replace('/', "\\")
        .to_lowercase();
    let Some(package_path) = normalized.strip_suffix("\\app\\chatgpt.exe") else {
        return false;
    };
    let mut parts = package_path.rsplit('\\');
    let package_name = parts.next().unwrap_or_default();
    let package_parent = parts.next().unwrap_or_default();
    package_parent == "windowsapps" && package_name.starts_with("openai.codex_")
}

pub fn looks_like_codex_process(name: &str, executable: &str, command_line: &[String]) -> bool {
    if command_line
        .iter()
        .any(|argument| argument.to_lowercase().starts_with("--type="))
    {
        return false;
    }
    let mut stems = HashSet::from([process_stem(name), process_stem(executable)]);
    stems.remove("");
    for excluded in ["codex-switcher", "codex_switcher", "codex.switcher"] {
        stems.remove(excluded);
    }
    if stems
        .iter()
        .any(|stem| stem == "codex" || stem.starts_with("codex-") || stem.starts_with("codex_"))
    {
        return true;
    }
    if stems.contains("chatgpt")
        && std::iter::once(executable)
            .chain(command_line.first().map(String::as_str))
            .any(is_codex_package_chatgpt_path)
    {
        return true;
    }
    if stems.iter().any(|stem| {
        matches!(
            stem.as_str(),
            "codexdesktop" | "codex-desktop" | "codex_desktop"
        ) || (stem.contains("codex") && stem.contains("desktop"))
    }) {
        return true;
    }
    let lowered = command_line.join(" ").to_lowercase();
    if lowered.contains("codexbot") {
        return false;
    }
    command_line
        .iter()
        .take(4)
        .any(|argument| matches!(process_stem(argument).as_str(), "codex" | "codex-cli"))
}

pub fn find_running_codex_processes() -> Result<Vec<u32>, CodexAccountError> {
    let current_pid = std::process::id();
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::All, true);
    let mut result: Vec<_> = system
        .processes()
        .values()
        .filter_map(|process| {
            let pid = process.pid().as_u32();
            if pid == 0 || pid == current_pid {
                return None;
            }
            let name = process.name().to_string_lossy();
            let executable = process.exe().map(Path::to_string_lossy).unwrap_or_default();
            let command_line: Vec<_> = process
                .cmd()
                .iter()
                .map(|value| value.to_string_lossy().into_owned())
                .collect();
            looks_like_codex_process(&name, &executable, &command_line).then_some(pid)
        })
        .collect();
    result.sort_unstable();
    result.dedup();
    Ok(result)
}

type ProcessChecker = dyn Fn() -> Result<Vec<u32>, CodexAccountError> + Send + Sync;

#[derive(Clone)]
pub struct CodexAccountManager {
    pub switcher_home: PathBuf,
    pub codex_home: PathBuf,
    process_checker: Arc<ProcessChecker>,
}

impl fmt::Debug for CodexAccountManager {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("CodexAccountManager")
            .field("switcher_home", &self.switcher_home)
            .field("codex_home", &self.codex_home)
            .finish_non_exhaustive()
    }
}

impl Default for CodexAccountManager {
    fn default() -> Self {
        Self::new()
    }
}

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

impl CodexAccountManager {
    pub fn new() -> Self {
        let switcher_home = std::env::var_os(CODEX_SWITCHER_HOME_ENV)
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(CODEX_SWITCHER_DIR_NAME));
        let codex_home = std::env::var_os("CODEX_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home_dir().join(".codex"));
        Self {
            switcher_home,
            codex_home,
            process_checker: Arc::new(find_running_codex_processes),
        }
    }

    pub fn with_paths(switcher_home: impl Into<PathBuf>, codex_home: impl Into<PathBuf>) -> Self {
        Self {
            switcher_home: switcher_home.into(),
            codex_home: codex_home.into(),
            process_checker: Arc::new(find_running_codex_processes),
        }
    }

    pub fn with_process_checker<F>(mut self, checker: F) -> Self
    where
        F: Fn() -> Result<Vec<u32>, CodexAccountError> + Send + Sync + 'static,
    {
        self.process_checker = Arc::new(checker);
        self
    }

    pub fn accounts_path(&self) -> PathBuf {
        self.switcher_home.join("accounts.json")
    }

    pub fn auth_path(&self) -> PathBuf {
        self.codex_home.join("auth.json")
    }

    pub fn load_store(&self) -> Result<CodexAccountStore, CodexAccountError> {
        let path = self.accounts_path();
        if !path.is_file() {
            return Ok(CodexAccountStore::default());
        }
        store_from_root(&read_json(&path)?)
    }

    pub fn has_saved_accounts(&self) -> Result<bool, CodexAccountError> {
        Ok(!self.load_store()?.accounts.is_empty())
    }

    fn read_current_auth(
        &self,
    ) -> Result<(Option<CodexTokens>, Option<String>), CodexAccountError> {
        let path = self.auth_path();
        if !path.is_file() {
            return Ok((None, None));
        }
        Ok(tokens_from_auth(&read_json(&path)?))
    }

    pub fn get_active_account(&self) -> Result<Option<CodexAccount>, CodexAccountError> {
        let store = self.load_store()?;
        let saved = store
            .active_account_id
            .as_ref()
            .and_then(|active| store.accounts.iter().find(|item| item.id == *active))
            .cloned();
        let (tokens, api_key) = self.read_current_auth()?;
        if let Some(mut account) = saved {
            if account.is_chatgpt()
                && tokens
                    .as_ref()
                    .is_some_and(|tokens| same_identity(&account, tokens))
            {
                let tokens = tokens.expect("checked above");
                let (email, plan, _) = jwt_claims(&tokens.id_token);
                account.email = email.or(account.email);
                account.plan = plan.or(account.plan);
                account.tokens = Some(tokens);
            } else if account.auth_type == "api_key" && api_key.is_some() {
                account.api_key = api_key;
            }
            return Ok(Some(account));
        }
        if let Some(tokens) = tokens {
            let (email, plan, account_id) = jwt_claims(&tokens.id_token);
            return Ok(Some(CodexAccount {
                id: account_id
                    .or_else(|| tokens.account_id.clone())
                    .unwrap_or_else(|| "current".to_owned()),
                name: email
                    .clone()
                    .unwrap_or_else(|| "当前 Codex 账号".to_owned()),
                email,
                plan,
                auth_type: "chatgpt".to_owned(),
                auth_state: "ready".to_owned(),
                tokens: Some(tokens),
                api_key: None,
            }));
        }
        Ok(api_key.map(|api_key| CodexAccount {
            id: "current".to_owned(),
            name: "当前 API key".to_owned(),
            email: None,
            plan: None,
            auth_type: "api_key".to_owned(),
            auth_state: "ready".to_owned(),
            tokens: None,
            api_key: Some(api_key),
        }))
    }

    pub fn list_accounts(&self) -> Result<Vec<CodexAccount>, CodexAccountError> {
        let store = self.load_store()?;
        if !store.accounts.is_empty() {
            return Ok(store.accounts);
        }
        Ok(self.get_active_account()?.into_iter().collect())
    }

    fn resolve_from(
        accounts: &[CodexAccount],
        selector: &str,
    ) -> Result<CodexAccount, CodexAccountError> {
        if accounts.is_empty() {
            return Err(CodexAccountError::new("没有发现 codex_login 保存的账号"));
        }
        let value = selector.trim();
        if value.is_empty() {
            return Err(CodexAccountError::new(
                "请指定账号序号、名称、邮箱或账号 ID",
            ));
        }
        if value.chars().all(|character| character.is_ascii_digit()) {
            let index = value.parse::<usize>().unwrap_or(0);
            return accounts.get(index.wrapping_sub(1)).cloned().ok_or_else(|| {
                CodexAccountError::new(format!("账号序号无效，可用范围：1-{}", accounts.len()))
            });
        }

        let exact: Vec<_> = accounts
            .iter()
            .filter(|account| {
                account.id.eq_ignore_ascii_case(value)
                    || account.name.eq_ignore_ascii_case(value)
                    || account
                        .email
                        .as_ref()
                        .is_some_and(|email| email.eq_ignore_ascii_case(value))
            })
            .cloned()
            .collect();
        if exact.len() == 1 {
            return Ok(exact[0].clone());
        }
        let folded = value.to_lowercase();
        let prefix: Vec<_> = accounts
            .iter()
            .filter(|account| account.id.to_lowercase().starts_with(&folded))
            .cloned()
            .collect();
        if prefix.len() == 1 {
            return Ok(prefix[0].clone());
        }
        if exact.len() > 1 || prefix.len() > 1 {
            return Err(CodexAccountError::new(
                "账号选择不唯一，请使用序号或完整账号 ID",
            ));
        }
        Err(CodexAccountError::new(format!(
            "找不到账号：{}",
            safe_display(Some(value), "未知账号", 100)
        )))
    }

    pub fn resolve_account(&self, selector: &str) -> Result<CodexAccount, CodexAccountError> {
        Self::resolve_from(&self.load_store()?.accounts, selector)
    }

    fn write_auth(&self, account: &CodexAccount) -> Result<(), CodexAccountError> {
        let payload = if account.auth_type == "api_key" {
            let key = account
                .api_key
                .as_ref()
                .ok_or_else(|| CodexAccountError::new("该账号缺少 API key"))?;
            json!({"OPENAI_API_KEY": key})
        } else if account.is_chatgpt() {
            let tokens = account.tokens.as_ref().expect("is_chatgpt checked");
            let mut token_value = json!({
                "id_token": tokens.id_token,
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
            });
            if let (Some(raw), Some(account_id)) =
                (token_value.as_object_mut(), tokens.account_id.as_ref())
            {
                raw.insert("account_id".to_owned(), Value::String(account_id.clone()));
            }
            json!({
                "tokens": token_value,
                "last_refresh": Utc::now().to_rfc3339_opts(SecondsFormat::AutoSi, true),
            })
        } else {
            return Err(CodexAccountError::new(
                "该账号不是可切换的 ChatGPT OAuth 或 API key 账号",
            ));
        };
        atomic_write_json(
            &self.auth_path(),
            payload.as_object().expect("object literal"),
            None,
        )
    }

    pub fn reconcile_active_account(&self) -> Result<(), CodexAccountError> {
        let accounts_path = self.accounts_path();
        if !accounts_path.is_file() || !self.auth_path().is_file() {
            return Ok(());
        }
        let Ok((current_tokens, _)) = self.read_current_auth() else {
            return Ok(());
        };
        update_account_store(&accounts_path, |root| {
            let store = store_from_root(root)?;
            let Some(active) = store
                .active_account_id
                .as_ref()
                .and_then(|active| store.accounts.iter().find(|account| account.id == *active))
            else {
                return Ok(false);
            };
            let Some(raw_accounts) = root.get_mut("accounts").and_then(Value::as_array_mut) else {
                return Ok(false);
            };
            let Some(raw) = raw_accounts.iter_mut().find_map(|item| {
                let object = item.as_object_mut()?;
                (text(object.get("id")).as_deref() == Some(active.id.as_str())).then_some(object)
            }) else {
                return Ok(false);
            };
            let auth_key = if raw.get("auth_data").is_some() {
                "auth_data"
            } else {
                "authData"
            };
            let Some(auth_data) = raw.get_mut(auth_key).and_then(Value::as_object_mut) else {
                return Ok(false);
            };
            let Some(tokens) = current_tokens
                .as_ref()
                .filter(|tokens| active.is_chatgpt() && same_identity(active, tokens))
            else {
                return Ok(false);
            };
            let mut changed = false;
            {
                let has_nested_tokens = auth_data.get("tokens").is_some_and(Value::is_object);
                let target = if has_nested_tokens {
                    auth_data
                        .get_mut("tokens")
                        .and_then(Value::as_object_mut)
                        .expect("checked object above")
                } else {
                    auth_data
                };
                for (key, value) in [
                    ("id_token", &tokens.id_token),
                    ("access_token", &tokens.access_token),
                    ("refresh_token", &tokens.refresh_token),
                ] {
                    if target.get(key).and_then(Value::as_str) != Some(value) {
                        target.insert(key.to_owned(), Value::String(value.clone()));
                        changed = true;
                    }
                }
                if let Some(account_id) = &tokens.account_id {
                    if target.get("account_id").and_then(Value::as_str) != Some(account_id) {
                        target.insert("account_id".to_owned(), Value::String(account_id.clone()));
                        changed = true;
                    }
                }
            }
            let (email, plan, _) = jwt_claims(&tokens.id_token);
            if let Some(email) = email
                && raw.get("email").and_then(Value::as_str) != Some(&email)
            {
                raw.insert("email".to_owned(), Value::String(email));
                changed = true;
            }
            if let Some(plan) = plan
                && raw.get("plan_type").and_then(Value::as_str) != Some(&plan)
            {
                raw.insert("plan_type".to_owned(), Value::String(plan));
                changed = true;
            }
            if raw.get("auth_state").and_then(Value::as_str) != Some("ready") {
                raw.insert("auth_state".to_owned(), Value::String("ready".to_owned()));
                changed = true;
            }
            Ok(changed)
        })
    }

    fn mark_active(&self, account_id: &str) -> Result<(), CodexAccountError> {
        let path = self.accounts_path();
        if !path.is_file() {
            return Ok(());
        }
        update_account_store(&path, |root| {
            let Some(accounts) = root.get_mut("accounts").and_then(Value::as_array_mut) else {
                return Err(CodexAccountError::new("账号在保存过程中已消失，请重试"));
            };
            if !accounts.iter().any(|item| {
                item.as_object()
                    .and_then(|raw| text(raw.get("id")))
                    .as_deref()
                    == Some(account_id)
            }) {
                return Err(CodexAccountError::new("账号在保存过程中已消失，请重试"));
            }
            let timestamp = Utc::now().to_rfc3339_opts(SecondsFormat::AutoSi, true);
            for item in accounts {
                let Some(raw) = item.as_object_mut() else {
                    continue;
                };
                if text(raw.get("id")).as_deref() == Some(account_id) {
                    raw.insert("last_used_at".to_owned(), Value::String(timestamp.clone()));
                }
            }
            root.insert(
                "active_account_id".to_owned(),
                Value::String(account_id.to_owned()),
            );
            root.remove("activeAccountId");
            Ok(true)
        })
    }

    pub fn switch_account(&self, selector: &str) -> Result<CodexAccount, CodexAccountError> {
        static SWITCH_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        let lock = SWITCH_LOCK.get_or_init(|| Mutex::new(()));
        let deadline = Instant::now() + Duration::from_secs(2);
        let guard = loop {
            if let Ok(guard) = lock.try_lock() {
                break guard;
            }
            if Instant::now() >= deadline {
                return Err(CodexAccountError::new(
                    "另一个 Codex 账号切换正在进行，请稍后重试",
                ));
            }
            thread::sleep(Duration::from_millis(20));
        };
        let result = self.switch_account_unlocked(selector);
        drop(guard);
        result
    }

    fn switch_account_unlocked(&self, selector: &str) -> Result<CodexAccount, CodexAccountError> {
        self.resolve_account(selector)?;
        let running = (self.process_checker)()?;
        if !running.is_empty() {
            return Err(CodexAccountError::new(format!(
                "检测到 {} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号",
                running.len()
            )));
        }
        self.reconcile_active_account()?;
        let account = self.resolve_account(selector)?;
        if !account.is_ready() {
            return Err(CodexAccountError::new(
                "该账号登录已过期，请先在 codex_login 中重新认证",
            ));
        }
        let running = (self.process_checker)()?;
        if !running.is_empty() {
            return Err(CodexAccountError::new(format!(
                "检测到 {} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号",
                running.len()
            )));
        }

        let auth_path = self.auth_path();
        let previous_auth = if auth_path.is_file() {
            Some(std::fs::read(&auth_path).map_err(|error| {
                CodexAccountError::new(format!("无法备份当前 Codex 登录：{}", error.kind()))
            })?)
        } else {
            None
        };
        self.write_auth(&account)?;
        if let Err(error) = self.mark_active(&account.id) {
            let rollback = match previous_auth {
                Some(previous) => atomic_write_bytes(&auth_path, &previous),
                None => match std::fs::remove_file(&auth_path) {
                    Ok(()) => Ok(()),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                    Err(error) => Err(CodexAccountError::from(error)),
                },
            };
            if rollback.is_err() {
                return Err(CodexAccountError::new(
                    "账号目录同步失败，并且原 Codex 登录也无法恢复；请在 codex_login 中重新选择账号",
                ));
            }
            return Err(error);
        }
        Ok(account)
    }
}

pub fn format_account_list(accounts: &[CodexAccount], active_id: Option<&str>) -> String {
    if accounts.is_empty() {
        return "没有发现 codex_login 保存的账号。".to_owned();
    }
    let mut lines = vec!["codex_login 已保存账号：".to_owned()];
    for (index, account) in accounts.iter().enumerate() {
        let mut markers = Vec::new();
        if active_id == Some(account.id.as_str()) {
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
            safe_display(Some(&account.name), "未知", 120),
            safe_display(account.email.as_deref(), "无邮箱", 120),
            suffix
        ));
    }
    lines.join("\n")
}

pub fn format_saved_account_text(account: &CodexAccount) -> String {
    let auth = if account.auth_type == "chatgpt" {
        "ChatGPT 登录"
    } else {
        "API key"
    };
    [
        "Codex 当前账号（codex_login）".to_owned(),
        format!("名称：{}", safe_display(Some(&account.name), "未知", 120)),
        format!(
            "邮箱：{}",
            safe_display(account.email.as_deref(), "未知", 120)
        ),
        format!(
            "套餐：{}",
            safe_display(account.plan.as_deref(), "未知", 120)
        ),
        format!("认证类型：{auth}"),
        format!(
            "状态：{}",
            if account.is_ready() {
                "正常"
            } else {
                "需要重新登录"
            }
        ),
    ]
    .join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use tempfile::TempDir;

    fn id_token(email: &str, account_id: &str) -> String {
        let payload = URL_SAFE_NO_PAD.encode(
            serde_json::to_vec(&json!({
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": account_id,
                    "chatgpt_plan_type": "plus"
                }
            }))
            .unwrap(),
        );
        format!("header.{payload}.signature")
    }

    fn write_store(path: &Path) {
        std::fs::create_dir_all(path).unwrap();
        std::fs::write(
            path.join("accounts.json"),
            serde_json::to_vec(&json!({
                "accounts": [
                    {
                        "id": "one",
                        "name": "one@example.com",
                        "email": "one@example.com",
                        "auth_mode": "chat_g_p_t",
                        "auth_data": {
                            "type": "chat_g_p_t",
                            "id_token": id_token("one@example.com", "one"),
                            "access_token": "access-one",
                            "refresh_token": "refresh-one",
                            "account_id": "one"
                        },
                        "auth_state": "ready"
                    },
                    {
                        "id": "two",
                        "name": "two@example.com",
                        "email": "two@example.com",
                        "auth_mode": "chat_g_p_t",
                        "auth_data": {
                            "type": "chat_g_p_t",
                            "id_token": id_token("two@example.com", "two"),
                            "access_token": "access-two",
                            "refresh_token": "refresh-two",
                            "account_id": "two"
                        },
                        "auth_state": "ready"
                    }
                ],
                "active_account_id": "one"
            }))
            .unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn loads_resolves_and_switches() {
        let temp = TempDir::new().unwrap();
        let switcher = temp.path().join("switcher");
        let codex = temp.path().join("codex");
        write_store(&switcher);
        let manager = CodexAccountManager::with_paths(&switcher, &codex)
            .with_process_checker(|| Ok(Vec::new()));
        assert_eq!(manager.resolve_account("2").unwrap().id, "two");
        assert_eq!(
            manager.resolve_account("two@example.com").unwrap().id,
            "two"
        );
        let switched = manager.switch_account("2").unwrap();
        assert_eq!(switched.id, "two");
        let auth: Value =
            serde_json::from_slice(&std::fs::read(manager.auth_path()).unwrap()).unwrap();
        assert_eq!(auth["tokens"]["account_id"], "two");
        assert_eq!(
            manager.load_store().unwrap().active_account_id.as_deref(),
            Some("two")
        );
    }

    #[test]
    fn debug_never_contains_credentials() {
        let token = CodexTokens {
            id_token: "id-secret".to_owned(),
            access_token: "access-secret".to_owned(),
            refresh_token: "refresh-secret".to_owned(),
            account_id: Some("account".to_owned()),
        };
        let output = format!("{token:?}");
        assert!(!output.contains("access-secret"));
        assert!(!output.contains("refresh-secret"));
    }

    #[test]
    fn process_classifier_is_conservative_for_chatgpt() {
        assert!(!looks_like_codex_process(
            "ChatGPT.exe",
            r"C:\WindowsApps\OpenAI.ChatGPT_1\app\ChatGPT.exe",
            &[]
        ));
        assert!(looks_like_codex_process(
            "ChatGPT.exe",
            r"C:\WindowsApps\OpenAI.Codex_1\app\ChatGPT.exe",
            &[]
        ));
    }
}
