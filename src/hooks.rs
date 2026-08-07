//! Neutral Codex hook ingestion entry point.

use crate::logging_utils::configure_logging;
use crate::paths::{database_path, ensure_data_dir};
use crate::processes::{discover_codex_host, ensure_daemon};
use crate::security::redact_secrets;
use crate::store::Store;
use anyhow::{Context, Result};
use serde_json::Value;
use std::collections::HashSet;
use std::env;
use std::io::{Read, Write};

pub const ANCESTOR_PIDS_ENV: &str = "CODEXBOT_HOOK_ANCESTOR_PIDS";
pub const HOOK_PAYLOAD_MAX_BYTES: usize = 128 * 1024;

fn parse_ancestor_pids(raw: &str) -> Vec<u32> {
    let mut seen = HashSet::new();
    raw.split(',')
        .take(32)
        .filter_map(|value| value.trim().parse::<u32>().ok())
        .filter(|pid| *pid > 0 && seen.insert(*pid))
        .collect()
}

/// Read the bounded ancestor snapshot supplied by an optional hook launcher.
pub fn ancestor_pids_from_environment() -> Vec<u32> {
    let raw = env::var(ANCESTOR_PIDS_ENV).unwrap_or_default();
    parse_ancestor_pids(&raw)
}

pub fn process_hook(payload: &Value, store: &Store, ancestor_pids: &[u32]) -> Result<bool> {
    let host = discover_codex_host(None, ancestor_pids);
    let inserted = store
        .ingest_hook(payload, host.as_ref())
        .context("failed to persist hook event")?;
    let event_name = payload.get("hook_event_name").and_then(Value::as_str);
    let lifecycle_host = host.is_some() && event_name != Some("SessionEnd");
    if env::var("CODEXBOT_DISABLE_DAEMON").as_deref() != Ok("1")
        && (lifecycle_host || store.companion_work_pending()?)
    {
        ensure_daemon(store, false).context("failed to start companion daemon")?;
    }
    Ok(inserted)
}

fn bounded_error(error: &anyhow::Error) -> String {
    redact_secrets(&error.to_string())
        .chars()
        .take(300)
        .collect()
}

fn read_hook_payload<R: Read>(input: &mut R) -> Result<Vec<u8>> {
    let mut raw = Vec::new();
    input
        .take((HOOK_PAYLOAD_MAX_BYTES + 1) as u64)
        .read_to_end(&mut raw)
        .context("failed to read hook input")?;
    if raw.len() > HOOK_PAYLOAD_MAX_BYTES {
        anyhow::bail!("hook payload exceeded {HOOK_PAYLOAD_MAX_BYTES} bytes");
    }
    Ok(raw)
}

/// Process one hook payload while preserving Codex's neutral hook contract.
///
/// Payload, storage, discovery, or daemon failures are logged without private
/// message content. The function still writes `{}` and returns success; only
/// an inability to write that neutral response is surfaced to the caller.
pub fn run_from<R: Read, W: Write>(mut input: R, mut output: W) -> std::io::Result<()> {
    let logger = ensure_data_dir()
        .and_then(|_| configure_logging("codexbot.hooks", false))
        .ok();
    let result = (|| -> Result<()> {
        let raw = read_hook_payload(&mut input)?;
        let payload: Value = serde_json::from_slice(&raw).context("invalid hook JSON")?;
        if !payload.is_object() {
            anyhow::bail!("hook payload must be a JSON object");
        }
        let store = Store::new(database_path()).context("failed to open state store")?;
        let ancestors = ancestor_pids_from_environment();
        process_hook(&payload, &store, &ancestors)?;
        Ok(())
    })();
    if let Err(error) = result {
        if let Some(logger) = logger {
            let _ = logger.error(&format!(
                "Hook processing failed: {}: {}",
                error.root_cause(),
                bounded_error(&error)
            ));
        }
    }
    output.write_all(b"{}")?;
    output.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ancestor_parser_deduplicates_invalid_values() {
        assert_eq!(parse_ancestor_pids("303,bad,404,303,0"), vec![303, 404]);
    }

    #[test]
    fn hook_payload_reader_is_bounded() {
        let input = vec![b'x'; HOOK_PAYLOAD_MAX_BYTES + 1];
        assert!(read_hook_payload(&mut &input[..]).is_err());
    }
}
