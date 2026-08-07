//! Secret redaction, pairing codes, and QQ credential persistence.

use regex::{Captures, Regex};
use sha2::{Digest, Sha256};
use std::error::Error;
use std::fmt;
use std::sync::OnceLock;
use unicode_normalization::char::canonical_combining_class;

pub const PAIRING_ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CREDENTIAL_SERVICE: &str = "CodexBot.QQ";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Credentials {
    pub app_id: String,
    pub app_secret: String,
}

#[derive(Debug)]
pub enum SecurityError {
    EmptyCredentials,
    UnsupportedPlatform,
    System(std::io::Error),
    InvalidCredentialData,
}

impl fmt::Display for SecurityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::EmptyCredentials => f.write_str("AppID 和 AppSecret 均不能为空"),
            Self::UnsupportedPlatform => {
                f.write_str("credential storage is only available on Windows")
            }
            Self::System(error) => write!(f, "credential manager error: {error}"),
            Self::InvalidCredentialData => f.write_str("credential data is not valid UTF-8"),
        }
    }
}

impl Error for SecurityError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::System(error) => Some(error),
            _ => None,
        }
    }
}

fn json_secret_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(
            r#"(?i)(?P<prefix>"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|app[_-]?secret|client[_-]?secret|password|passwd|token|secret)"\s*:\s*")(?P<value>(?:\\.|[^"\\])*)(?P<suffix>")"#,
        )
        .expect("static JSON secret regex is valid")
    })
}

fn openai_secret_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"\bsk-[A-Za-z0-9_-]{12,}\b").expect("static API-key regex is valid")
    })
}

fn bearer_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*").expect("static bearer regex is valid")
    })
}

fn assigned_secret_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(
            r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|app[_-]?secret|client[_-]?secret|password|passwd|token|secret)(\s*[:=]\s*)([^\s,;]+)",
        )
        .expect("static assigned-secret regex is valid")
    })
}

pub fn redact_secrets(text: &str) -> String {
    let json_redacted = json_secret_pattern().replace_all(text, |captures: &Captures<'_>| {
        format!("{}[REDACTED]{}", &captures["prefix"], &captures["suffix"])
    });
    let api_redacted = openai_secret_pattern().replace_all(&json_redacted, "[REDACTED]");
    let bearer_redacted = bearer_pattern().replace_all(&api_redacted, "Bearer [REDACTED]");
    assigned_secret_pattern()
        .replace_all(&bearer_redacted, |captures: &Captures<'_>| {
            format!("{}{}[REDACTED]", &captures[1], &captures[2])
        })
        .into_owned()
}

fn unsafe_boundary(value: char) -> bool {
    canonical_combining_class(value) != 0 || matches!(value, '\u{200d}' | '\u{fe0e}' | '\u{fe0f}')
}

pub fn prompt_preview(text: &str, limit: usize) -> String {
    let compact = text.split_whitespace().collect::<Vec<_>>().join(" ");
    let normalized = redact_secrets(&compact);
    let characters: Vec<char> = normalized.chars().collect();
    if characters.len() <= limit {
        return normalized;
    }

    let mut cut = limit.saturating_sub(1).max(1);
    while cut > 1 && cut < characters.len() {
        let current = characters[cut];
        let previous = characters[cut - 1];
        if unsafe_boundary(current) || current == '\u{200d}' || previous == '\u{200d}' {
            cut -= 1;
        } else {
            break;
        }
    }
    let prefix: String = characters[..cut].iter().collect();
    format!("{}…", prefix.trim_end())
}

pub fn generate_pairing_code() -> String {
    use rand::Rng;

    let mut rng = rand::thread_rng();
    let raw: String = (0..8)
        .map(|_| {
            let index = rng.gen_range(0..PAIRING_ALPHABET.len());
            char::from(PAIRING_ALPHABET[index])
        })
        .collect();
    format!("{}-{}", &raw[..4], &raw[4..])
}

pub fn normalize_pairing_code(code: &str) -> String {
    code.chars()
        .flat_map(char::to_uppercase)
        .filter(char::is_ascii_alphanumeric)
        .collect()
}

pub fn hash_pairing_code(code: &str) -> String {
    let normalized = normalize_pairing_code(code);
    let digest = Sha256::digest(normalized.as_bytes());
    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").expect("writing to a String cannot fail");
    }
    encoded
}

#[cfg(windows)]
fn fallback_credential_target(account: &str) -> String {
    format!("{CREDENTIAL_SERVICE}/{account}")
}

#[cfg(windows)]
fn compound_credential_target(account: &str) -> String {
    format!("{account}@{CREDENTIAL_SERVICE}")
}

#[cfg(windows)]
fn write_windows_credential(target: &str, account: &str, value: &str) -> Result<(), SecurityError> {
    use std::ptr::null_mut;
    use windows_sys::Win32::Security::Credentials::{
        CRED_PERSIST_ENTERPRISE, CRED_TYPE_GENERIC, CREDENTIALW, CredWriteW,
    };

    let mut target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let mut username: Vec<u16> = account.encode_utf16().chain(Some(0)).collect();
    let mut comment: Vec<u16> = "Stored using python-keyring"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    // pywin32-ctypes passes a Python `str` to CredWriteW as a wide string and
    // excludes only the trailing NUL from CredentialBlobSize.
    let mut blob: Vec<u16> = value.encode_utf16().collect();
    let credential = CREDENTIALW {
        Flags: 0,
        Type: CRED_TYPE_GENERIC,
        TargetName: target.as_mut_ptr(),
        Comment: comment.as_mut_ptr(),
        LastWritten: windows_sys::Win32::Foundation::FILETIME {
            dwLowDateTime: 0,
            dwHighDateTime: 0,
        },
        CredentialBlobSize: (blob.len() * std::mem::size_of::<u16>()) as u32,
        CredentialBlob: blob.as_mut_ptr().cast(),
        Persist: CRED_PERSIST_ENTERPRISE,
        AttributeCount: 0,
        Attributes: null_mut(),
        TargetAlias: null_mut(),
        UserName: username.as_mut_ptr(),
    };
    // SAFETY: all pointers in `credential` refer to live buffers for the
    // duration of the synchronous call, and every UTF-16 string is NUL-terminated.
    if unsafe { CredWriteW(&credential, 0) } == 0 {
        return Err(SecurityError::System(std::io::Error::last_os_error()));
    }
    Ok(())
}

#[cfg(windows)]
fn decode_utf16_blob(bytes: &[u8]) -> Option<String> {
    if bytes.len() % 2 != 0 {
        return None;
    }
    let wide: Vec<u16> = bytes
        .chunks_exact(2)
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect();
    String::from_utf16(&wide).ok()
}

#[cfg(windows)]
fn decode_credential_blob(bytes: &[u8], legacy_utf8_first: bool) -> Result<String, SecurityError> {
    let utf8 = || std::str::from_utf8(bytes).ok().map(str::to_owned);
    let decoded = if legacy_utf8_first {
        utf8().or_else(|| decode_utf16_blob(bytes))
    } else {
        decode_utf16_blob(bytes).or_else(utf8)
    };
    decoded.ok_or(SecurityError::InvalidCredentialData)
}

#[cfg(windows)]
fn wide_pointer_to_string(pointer: *const u16) -> String {
    if pointer.is_null() {
        return String::new();
    }
    let mut length = 0usize;
    // SAFETY: Credential Manager returns UserName as a valid NUL-terminated
    // string whose allocation remains live until CredFree.
    unsafe {
        while *pointer.add(length) != 0 {
            length += 1;
        }
        String::from_utf16_lossy(std::slice::from_raw_parts(pointer, length))
    }
}

#[cfg(windows)]
fn read_windows_credential(
    target: &str,
    expected_account: &str,
    legacy_utf8_first: bool,
) -> Result<Option<String>, SecurityError> {
    use std::ptr::null_mut;
    use std::slice;
    use windows_sys::Win32::Foundation::{ERROR_NOT_FOUND, GetLastError};
    use windows_sys::Win32::Security::Credentials::{
        CRED_TYPE_GENERIC, CREDENTIALW, CredFree, CredReadW,
    };

    let target: Vec<u16> = target.encode_utf16().chain(Some(0)).collect();
    let mut raw: *mut CREDENTIALW = null_mut();
    // SAFETY: `target` is NUL-terminated and `raw` points to writable storage.
    if unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut raw) } == 0 {
        // SAFETY: GetLastError has no preconditions and is called immediately
        // after the failed Win32 call.
        let code = unsafe { GetLastError() };
        if code == ERROR_NOT_FOUND {
            return Ok(None);
        }
        return Err(SecurityError::System(std::io::Error::from_raw_os_error(
            code as i32,
        )));
    }
    if raw.is_null() {
        return Ok(None);
    }

    // SAFETY: a successful CredReadW returns a CREDENTIALW allocated by the
    // system. Its blob is valid until the matching CredFree below.
    let result = unsafe {
        let credential = &*raw;
        let username = wide_pointer_to_string(credential.UserName);
        if username != expected_account {
            Ok(None)
        } else {
            let bytes = if credential.CredentialBlobSize == 0 {
                &[][..]
            } else {
                slice::from_raw_parts(
                    credential.CredentialBlob,
                    credential.CredentialBlobSize as usize,
                )
            };
            decode_credential_blob(bytes, legacy_utf8_first).map(Some)
        }
    };
    // SAFETY: `raw` came from CredReadW and has not previously been freed.
    unsafe { CredFree(raw.cast()) };
    result
}

#[cfg(windows)]
fn resolve_windows_credential(account: &str) -> Result<Option<String>, SecurityError> {
    // WinVaultKeyring first checks the bare service target, validates its
    // UserName, then falls back to `{username}@{service}` for collisions.
    if let Some(value) = read_windows_credential(CREDENTIAL_SERVICE, account, false)? {
        return Ok(Some(value));
    }
    if let Some(value) =
        read_windows_credential(&compound_credential_target(account), account, false)?
    {
        return Ok(Some(value));
    }
    // Builds produced during the initial Rust migration used one UTF-8 target
    // per account. Retain a read-only fallback so those credentials survive.
    read_windows_credential(&fallback_credential_target(account), account, true)
}

pub fn store_credentials(app_id: &str, app_secret: &str) -> Result<(), SecurityError> {
    let app_id = app_id.trim();
    let app_secret = app_secret.trim();
    if app_id.is_empty() || app_secret.is_empty() {
        return Err(SecurityError::EmptyCredentials);
    }
    #[cfg(windows)]
    {
        // This is the final layout produced by sequential WinVaultKeyring
        // writes: the first username is moved to its compound target when the
        // second username claims the bare service target.
        write_windows_credential(&compound_credential_target("app_id"), "app_id", app_id)?;
        write_windows_credential(CREDENTIAL_SERVICE, "app_secret", app_secret)?;
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = (app_id, app_secret);
        Err(SecurityError::UnsupportedPlatform)
    }
}

pub fn load_credentials() -> Result<Option<Credentials>, SecurityError> {
    #[cfg(windows)]
    {
        let app_id = resolve_windows_credential("app_id")?;
        let app_secret = resolve_windows_credential("app_secret")?;
        Ok(match (app_id, app_secret) {
            (Some(app_id), Some(app_secret)) if !app_id.is_empty() && !app_secret.is_empty() => {
                Some(Credentials { app_id, app_secret })
            }
            _ => None,
        })
    }
    #[cfg(not(windows))]
    {
        let app_id = std::env::var("CODEXBOT_APP_ID").ok();
        let app_secret = std::env::var("CODEXBOT_APP_SECRET").ok();
        Ok(match (app_id, app_secret) {
            (Some(app_id), Some(app_secret)) if !app_id.is_empty() && !app_secret.is_empty() => {
                Some(Credentials { app_id, app_secret })
            }
            _ => None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn common_secret_shapes_are_redacted() {
        let source = concat!(
            "sk-abcdefghijklmnopqrstuvwxyz ",
            "Bearer abc.def-123 ",
            "api_key=super-secret appSecret:qq-secret password=hunter2"
        );
        let result = redact_secrets(source);
        assert_eq!(result.matches("[REDACTED]").count(), 5);
        assert!(!result.contains("super-secret"));
    }

    #[test]
    fn quoted_json_values_are_redacted_as_a_unit() {
        let source = r#"{"api_key": "json-api-secret,with punctuation", "appSecret":"secret"}"#;
        let result = redact_secrets(source);
        assert_eq!(result.matches("[REDACTED]").count(), 2);
        assert!(result.contains(r#""api_key": "[REDACTED]""#));
    }

    #[test]
    fn preview_is_bounded_and_pairing_is_separator_insensitive() {
        let source = format!("  请处理\n\n{}token=do-not-store", "资料👨‍👩‍👧‍👦 ".repeat(40));
        let result = prompt_preview(&source, 120);
        assert!(result.chars().count() <= 120);
        assert!(result.ends_with('…'));
        assert_eq!(
            hash_pairing_code("ABCD-EF23"),
            hash_pairing_code("abcd ef23")
        );
    }

    #[cfg(windows)]
    #[test]
    fn winvault_targets_and_utf16_blob_match_python_keyring() {
        assert_eq!(compound_credential_target("app_id"), "app_id@CodexBot.QQ");
        assert_eq!(fallback_credential_target("app_id"), "CodexBot.QQ/app_id");
        let bytes: Vec<u8> = "应用密钥"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        assert_eq!(decode_credential_blob(&bytes, false).unwrap(), "应用密钥");
    }
}
