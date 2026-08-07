//! DPAPI-encrypted snapshots of the active Codex `auth.json`.

use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use chrono::Local;
use rand::RngCore;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::ffi::OsStr;
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;

use crate::codex_accounts::find_running_codex_processes;
use crate::paths::data_dir;

pub const ACCOUNTS_SUBDIR: &str = "accounts";
pub const BACKUPS_SUBDIR: &str = "backups";

#[derive(Debug, Error)]
pub enum AccountSwitchError {
    #[error("~/.codex/auth.json 不存在，请先在 Codex 中登录。")]
    NotLoggedIn,
    #[error("账号名称不能为空。")]
    AccountName,
    #[error("{0}")]
    SnapshotNotFound(String),
    #[error("{0}")]
    InvalidSnapshot(String),
    #[error("CodexBot 账号快照只支持 Windows DPAPI。")]
    UnsupportedPlatform,
    #[error("{0}")]
    Other(String),
    #[error("账号文件操作失败：{0}")]
    Io(#[from] io::Error),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AccountSnapshot {
    pub name: String,
    pub email: Option<String>,
    pub account_id: Option<String>,
    pub saved_at: f64,
}

#[derive(Debug, Clone)]
pub struct AccountSnapshotStore {
    pub auth_path: PathBuf,
    pub accounts_dir: PathBuf,
}

fn home_dir() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn auth_file_path() -> PathBuf {
    std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home_dir().join(".codex"))
        .join("auth.json")
}

pub fn accounts_dir() -> PathBuf {
    data_dir().join(ACCOUNTS_SUBDIR)
}

pub fn backups_dir() -> PathBuf {
    accounts_dir().join(BACKUPS_SUBDIR)
}

impl Default for AccountSnapshotStore {
    fn default() -> Self {
        Self {
            auth_path: auth_file_path(),
            accounts_dir: accounts_dir(),
        }
    }
}

impl AccountSnapshotStore {
    pub fn new(auth_path: impl Into<PathBuf>, accounts_dir: impl Into<PathBuf>) -> Self {
        Self {
            auth_path: auth_path.into(),
            accounts_dir: accounts_dir.into(),
        }
    }

    pub fn backups_dir(&self) -> PathBuf {
        self.accounts_dir.join(BACKUPS_SUBDIR)
    }

    pub fn snapshot_path(&self, name: &str) -> PathBuf {
        snapshot_path_in(&self.accounts_dir, name)
    }

    pub fn legacy_snapshot_path(&self, name: &str) -> PathBuf {
        self.accounts_dir.join(format!("{}.enc", slugify(name)))
    }

    fn read_auth_json(&self) -> Result<Map<String, Value>, AccountSwitchError> {
        if !self.auth_path.is_file() {
            return Err(AccountSwitchError::NotLoggedIn);
        }
        let value: Value =
            serde_json::from_slice(&std::fs::read(&self.auth_path)?).map_err(|error| {
                AccountSwitchError::Other(format!("auth.json 无法解析：{:?}", error.classify()))
            })?;
        let root = value.as_object().cloned().ok_or_else(|| {
            AccountSwitchError::Other("auth.json 的根节点必须是对象。".to_owned())
        })?;
        let has_tokens = root.get("tokens").is_some_and(Value::is_object);
        let has_api_key = root
            .get("OPENAI_API_KEY")
            .or_else(|| root.get("openai_api_key"))
            .is_some_and(Value::is_string);
        if !has_tokens && !has_api_key {
            return Err(AccountSwitchError::Other(
                "auth.json 既不包含 ChatGPT tokens，也不包含 API key，无法保存。".to_owned(),
            ));
        }
        Ok(root)
    }

    pub fn save_current_account(&self, name: &str) -> Result<AccountSnapshot, AccountSwitchError> {
        let display_name = name.trim();
        if display_name.is_empty() {
            return Err(AccountSwitchError::AccountName);
        }
        let auth_json = self.read_auth_json()?;
        let (email, account_id) = account_identity(&auth_json);
        let snapshot = AccountSnapshot {
            name: display_name.to_owned(),
            email,
            account_id,
            saved_at: unix_now(),
        };
        let payload = serde_json::json!({
            "name": snapshot.name,
            "email": snapshot.email,
            "account_id": snapshot.account_id,
            "saved_at": snapshot.saved_at,
            "auth_json": Value::Object(auth_json),
        });
        let plain = serde_json::to_vec(&payload)
            .map_err(|error| AccountSwitchError::Other(format!("账号快照无法序列化：{error}")))?;
        let encrypted = dpapi_protect(&plain)?;
        write_atomic(&self.snapshot_path(display_name), &encrypted)?;
        Ok(snapshot)
    }

    fn load_snapshot_path(&self, path: &Path) -> Result<Map<String, Value>, AccountSwitchError> {
        let payload = std::fs::read(path)
            .map_err(AccountSwitchError::Io)
            .and_then(|value| dpapi_unprotect(&value))
            .and_then(|plain| {
                serde_json::from_slice::<Value>(&plain).map_err(|_| {
                    AccountSwitchError::InvalidSnapshot(format!(
                        "账号快照 {:?} 无法解密或解析。",
                        path.file_stem().unwrap_or_default()
                    ))
                })
            })?;
        let root = payload.as_object().cloned().ok_or_else(|| {
            AccountSwitchError::InvalidSnapshot(format!(
                "账号快照 {:?} 格式无效。",
                path.file_stem().unwrap_or_default()
            ))
        })?;
        if !root.get("auth_json").is_some_and(Value::is_object) {
            return Err(AccountSwitchError::InvalidSnapshot(format!(
                "账号快照 {:?} 格式无效。",
                path.file_stem().unwrap_or_default()
            )));
        }
        Ok(root)
    }

    fn load_snapshot(&self, name: &str) -> Result<Map<String, Value>, AccountSwitchError> {
        let display_name = name.trim();
        for path in [
            self.snapshot_path(display_name),
            self.legacy_snapshot_path(display_name),
        ] {
            if !path.is_file() {
                continue;
            }
            let payload = self.load_snapshot_path(&path)?;
            let stored_name = string_field(&payload, "name").unwrap_or_else(|| {
                path.file_stem()
                    .and_then(OsStr::to_str)
                    .unwrap_or_default()
                    .to_owned()
            });
            if stored_name.eq_ignore_ascii_case(display_name) {
                return Ok(payload);
            }
        }

        if let Ok(entries) = std::fs::read_dir(&self.accounts_dir) {
            for path in entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .filter(|path| path.extension() == Some(OsStr::new("enc")))
            {
                let Ok(payload) = self.load_snapshot_path(&path) else {
                    continue;
                };
                if string_field(&payload, "name")
                    .is_some_and(|stored| stored.eq_ignore_ascii_case(display_name))
                {
                    return Ok(payload);
                }
            }
        }
        Err(AccountSwitchError::SnapshotNotFound(format!(
            "未找到已保存的账号 {name:?}，请先用 /account save 保存。"
        )))
    }

    pub fn list_accounts(&self) -> Result<Vec<AccountSnapshot>, AccountSwitchError> {
        std::fs::create_dir_all(&self.accounts_dir)?;
        let mut by_name: BTreeMap<String, AccountSnapshot> = BTreeMap::new();
        let mut paths: Vec<_> = std::fs::read_dir(&self.accounts_dir)?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| path.extension() == Some(OsStr::new("enc")))
            .collect();
        paths.sort();
        for path in paths {
            let Ok(payload) = self.load_snapshot_path(&path) else {
                continue;
            };
            let Some(saved_at) = number_field(&payload, "saved_at") else {
                continue;
            };
            let snapshot = AccountSnapshot {
                name: string_field(&payload, "name").unwrap_or_else(|| {
                    path.file_stem()
                        .and_then(OsStr::to_str)
                        .unwrap_or_default()
                        .to_owned()
                }),
                email: optional_string_field(&payload, "email"),
                account_id: optional_string_field(&payload, "account_id"),
                saved_at,
            };
            let key = snapshot.name.to_lowercase();
            if by_name
                .get(&key)
                .is_none_or(|previous| snapshot.saved_at >= previous.saved_at)
            {
                by_name.insert(key, snapshot);
            }
        }
        let mut result: Vec<_> = by_name.into_values().collect();
        result.sort_by(|left, right| {
            left.name
                .to_lowercase()
                .cmp(&right.name.to_lowercase())
                .then_with(|| left.saved_at.total_cmp(&right.saved_at))
        });
        Ok(result)
    }

    pub fn switch_account<F>(
        &self,
        name: &str,
        process_checker: F,
    ) -> Result<(Option<String>, Option<String>), AccountSwitchError>
    where
        F: FnOnce() -> Result<Vec<u32>, AccountSwitchError>,
    {
        let running = process_checker()?;
        if !running.is_empty() {
            return Err(AccountSwitchError::Other(format!(
                "检测到 {} 个 Codex/ChatGPT 进程正在运行，请完全退出后再切换账号。",
                running.len()
            )));
        }
        let payload = self.load_snapshot(name)?;
        let auth_json = payload
            .get("auth_json")
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| AccountSwitchError::InvalidSnapshot("账号快照格式无效。".to_owned()))?;

        if self.auth_path.is_file() {
            if let Ok(current) = self.read_auth_json() {
                let (email, account_id) = account_identity(&current);
                let backup = serde_json::json!({
                    "email": email,
                    "account_id": account_id,
                    "saved_at": unix_now(),
                    "auth_json": Value::Object(current),
                });
                let plain = serde_json::to_vec(&backup).map_err(|error| {
                    AccountSwitchError::Other(format!("账号备份无法序列化：{error}"))
                })?;
                let encrypted = dpapi_protect(&plain)?;
                let stamp = Local::now().format("%Y%m%d-%H%M%S");
                write_atomic(
                    &self
                        .backups_dir()
                        .join(format!("before-switch-{stamp}.enc")),
                    &encrypted,
                )?;
            }
        }

        let encoded = serde_json::to_vec(&Value::Object(auth_json.clone()))
            .map_err(|error| AccountSwitchError::Other(format!("Codex 登录无法序列化：{error}")))?;
        write_atomic(&self.auth_path, &encoded)?;
        Ok(account_identity(&auth_json))
    }

    pub fn delete_account(&self, name: &str) -> Result<(), AccountSwitchError> {
        let display_name = name.trim();
        let mut matches = HashSet::new();
        for candidate in [
            self.snapshot_path(display_name),
            self.legacy_snapshot_path(display_name),
        ] {
            if !candidate.is_file() {
                continue;
            }
            let Ok(payload) = self.load_snapshot_path(&candidate) else {
                continue;
            };
            let stored = string_field(&payload, "name").unwrap_or_else(|| {
                candidate
                    .file_stem()
                    .and_then(OsStr::to_str)
                    .unwrap_or_default()
                    .to_owned()
            });
            if stored.eq_ignore_ascii_case(display_name) {
                matches.insert(candidate);
            }
        }
        if let Ok(entries) = std::fs::read_dir(&self.accounts_dir) {
            for candidate in entries
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .filter(|path| path.extension() == Some(OsStr::new("enc")))
            {
                if matches.contains(&candidate) {
                    continue;
                }
                let Ok(payload) = self.load_snapshot_path(&candidate) else {
                    continue;
                };
                if string_field(&payload, "name")
                    .is_some_and(|stored| stored.eq_ignore_ascii_case(display_name))
                {
                    matches.insert(candidate);
                }
            }
        }
        if matches.is_empty() {
            return Err(AccountSwitchError::SnapshotNotFound(format!(
                "未找到已保存的账号 {name:?}。"
            )));
        }
        for path in matches {
            std::fs::remove_file(&path).map_err(|error| {
                AccountSwitchError::Other(format!("删除账号 {name:?} 失败：{}", error.kind()))
            })?;
        }
        Ok(())
    }

    pub fn current_account_email(&self) -> Option<String> {
        let data = self.read_auth_json().ok()?;
        account_identity(&data).0
    }
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn slugify(name: &str) -> String {
    let mut slug = String::new();
    let mut previous_underscore = false;
    for character in name.trim().chars() {
        if character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | '-') {
            slug.push(character);
            previous_underscore = false;
        } else if !previous_underscore {
            slug.push('_');
            previous_underscore = true;
        }
    }
    let slug = slug.trim_matches(['.', '_', '-']);
    if slug.is_empty() {
        "account".to_owned()
    } else {
        slug.to_owned()
    }
}

fn snapshot_path_in(accounts: &Path, name: &str) -> PathBuf {
    let normalized = name.trim().to_lowercase();
    let digest = Sha256::digest(normalized.as_bytes());
    accounts.join(format!(
        "{}--{}.enc",
        slugify(name),
        &hex::encode(digest)[..16]
    ))
}

fn string_field(root: &Map<String, Value>, key: &str) -> Option<String> {
    root.get(key)?.as_str().map(ToOwned::to_owned)
}

fn optional_string_field(root: &Map<String, Value>, key: &str) -> Option<String> {
    root.get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn number_field(root: &Map<String, Value>, key: &str) -> Option<f64> {
    let value = root.get(key)?;
    let number = value
        .as_f64()
        .or_else(|| value.as_str()?.parse::<f64>().ok())?;
    number.is_finite().then_some(number)
}

fn jwt_payload(token: &str) -> Map<String, Value> {
    let parts: Vec<_> = token.splitn(3, '.').collect();
    if parts.len() != 3 {
        return Map::new();
    }
    let mut payload = parts[1].to_owned();
    payload.extend(std::iter::repeat_n('=', (4 - payload.len() % 4) % 4));
    URL_SAFE
        .decode(payload.as_bytes())
        .ok()
        .and_then(|decoded| serde_json::from_slice::<Value>(&decoded).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default()
}

pub fn account_identity(data: &Map<String, Value>) -> (Option<String>, Option<String>) {
    let tokens = data.get("tokens").and_then(Value::as_object);
    let mut email = data
        .get("email")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let account_id = data
        .get("account_id")
        .and_then(Value::as_str)
        .or_else(|| tokens?.get("account_id")?.as_str())
        .map(ToOwned::to_owned);
    if email.is_none() {
        for token_name in ["id_token", "access_token"] {
            let Some(token) = tokens
                .and_then(|raw| raw.get(token_name))
                .and_then(Value::as_str)
            else {
                continue;
            };
            let payload = jwt_payload(token);
            email = payload
                .get("email")
                .or_else(|| payload.get("https://api.openai.com/auth.email"))
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            if email.is_some() {
                break;
            }
        }
    }
    (email, account_id)
}

fn temporary_path(target: &Path) -> PathBuf {
    let mut random = [0_u8; 8];
    rand::thread_rng().fill_bytes(&mut random);
    target.with_file_name(format!(".auth-switch-{}", hex::encode(random)))
}

#[cfg(windows)]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };
    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
    // SAFETY: the pointers refer to valid NUL-terminated UTF-16 buffers.
    if unsafe {
        MoveFileExW(
            source.as_ptr(),
            target.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    std::fs::rename(source, target)
}

fn write_atomic(target: &Path, content: &[u8]) -> Result<(), AccountSwitchError> {
    let parent = target
        .parent()
        .ok_or_else(|| AccountSwitchError::Other("目标文件没有父目录。".to_owned()))?;
    std::fs::create_dir_all(parent)?;
    let temporary = temporary_path(target);
    let result = (|| -> io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(content)?;
        file.sync_all()?;
        drop(file);
        replace_file(&temporary, target)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(target, std::fs::Permissions::from_mode(0o600))?;
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&temporary);
    }
    result.map_err(AccountSwitchError::Io)
}

#[cfg(windows)]
fn dpapi_protect(plain: &[u8]) -> Result<Vec<u8>, AccountSwitchError> {
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{CRYPT_INTEGER_BLOB, CryptProtectData};

    let input = CRYPT_INTEGER_BLOB {
        cbData: plain.len().try_into().map_err(|_| {
            AccountSwitchError::Other("账号快照过大，无法使用 DPAPI 加密。".to_owned())
        })?,
        pbData: plain.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };
    // SAFETY: input points to `plain`; output is initialized by DPAPI and
    // copied before being released with LocalFree.
    let ok = unsafe {
        CryptProtectData(
            &input,
            ptr::null(),
            ptr::null(),
            ptr::null_mut(),
            ptr::null(),
            0,
            &mut output,
        )
    };
    if ok == 0 {
        return Err(AccountSwitchError::Other(
            "DPAPI 加密失败（CryptProtectData）。".to_owned(),
        ));
    }
    // SAFETY: a successful call returns `cbData` valid bytes at `pbData`.
    let encrypted =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
    // SAFETY: DPAPI allocates this buffer using LocalAlloc.
    unsafe { LocalFree(output.pbData.cast()) };
    Ok(encrypted)
}

#[cfg(not(windows))]
fn dpapi_protect(_plain: &[u8]) -> Result<Vec<u8>, AccountSwitchError> {
    Err(AccountSwitchError::UnsupportedPlatform)
}

#[cfg(windows)]
fn dpapi_unprotect(encrypted: &[u8]) -> Result<Vec<u8>, AccountSwitchError> {
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{CRYPT_INTEGER_BLOB, CryptUnprotectData};

    let input = CRYPT_INTEGER_BLOB {
        cbData: encrypted.len().try_into().map_err(|_| {
            AccountSwitchError::Other("账号快照过大，无法使用 DPAPI 解密。".to_owned())
        })?,
        pbData: encrypted.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };
    let mut description = ptr::null_mut();
    // SAFETY: input points to `encrypted`; output and description are owned
    // by the caller after a successful return.
    let ok = unsafe {
        CryptUnprotectData(
            &input,
            &mut description,
            ptr::null(),
            ptr::null_mut(),
            ptr::null(),
            0,
            &mut output,
        )
    };
    if ok == 0 {
        return Err(AccountSwitchError::Other(
            "DPAPI 解密失败（CryptUnprotectData）。".to_owned(),
        ));
    }
    // SAFETY: successful DPAPI output is valid for `cbData` bytes.
    let plain =
        unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize).to_vec() };
    // SAFETY: both non-null results are allocated by LocalAlloc.
    unsafe {
        LocalFree(output.pbData.cast());
        if !description.is_null() {
            LocalFree(description.cast());
        }
    }
    Ok(plain)
}

#[cfg(not(windows))]
fn dpapi_unprotect(_encrypted: &[u8]) -> Result<Vec<u8>, AccountSwitchError> {
    Err(AccountSwitchError::UnsupportedPlatform)
}

pub fn save_current_account(name: &str) -> Result<AccountSnapshot, AccountSwitchError> {
    AccountSnapshotStore::default().save_current_account(name)
}

pub fn list_accounts() -> Result<Vec<AccountSnapshot>, AccountSwitchError> {
    AccountSnapshotStore::default().list_accounts()
}

pub fn switch_account(name: &str) -> Result<(Option<String>, Option<String>), AccountSwitchError> {
    AccountSnapshotStore::default().switch_account(name, || {
        find_running_codex_processes().map_err(|error| AccountSwitchError::Other(error.to_string()))
    })
}

pub fn delete_account(name: &str) -> Result<(), AccountSwitchError> {
    AccountSnapshotStore::default().delete_account(name)
}

pub fn current_account_email() -> Option<String> {
    AccountSnapshotStore::default().current_account_email()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn non_ascii_names_have_distinct_paths() {
        let root = Path::new("accounts");
        assert_ne!(
            snapshot_path_in(root, "工作账号"),
            snapshot_path_in(root, "个人账号")
        );
    }

    #[test]
    fn reads_nested_identity_from_jwt() {
        let body = URL_SAFE.encode(br#"{"email":"nested@example.com"}"#);
        let data = serde_json::json!({
            "tokens": {
                "id_token": format!("header.{body}.signature"),
                "account_id": "nested-account"
            }
        });
        let (email, id) = account_identity(data.as_object().unwrap());
        assert_eq!(email.as_deref(), Some("nested@example.com"));
        assert_eq!(id.as_deref(), Some("nested-account"));
    }
}
