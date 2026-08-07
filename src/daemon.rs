//! Long-lived CodexBot companion process lifecycle.

use crate::locks::FileLock;
use crate::logging_utils::{ProcessSafeLogger, configure_logging};
use crate::paths::{database_path, ensure_data_dir};
use crate::processes::{ensure_daemon, process_matches};
use crate::qq_client::run_qq_runtime;
use crate::security::{Credentials, load_credentials, redact_secrets};
use crate::store::Store;
use anyhow::{Context, Result};
use std::env;
use std::sync::Arc;
use std::time::Duration;
use sysinfo::{Pid, ProcessesToUpdate, System};
use tokio::sync::watch;

pub const CLEANUP_INTERVAL: Duration = Duration::from_secs(60 * 60);
const CREDENTIAL_POLL_INTERVAL: Duration = Duration::from_secs(1);
const EMPTY_HOST_CHECKS: u32 = 2;

fn safe_detail(error: &dyn std::fmt::Display) -> String {
    redact_secrets(&error.to_string())
        .chars()
        .take(300)
        .collect()
}

fn current_process_create_time() -> f64 {
    let pid = std::process::id();
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::Some(&[Pid::from_u32(pid)]), true);
    system
        .process(Pid::from_u32(pid))
        .map(|process| process.start_time() as f64)
        .unwrap_or(0.0)
}

fn lifecycle_work_remains(store: &Store, standalone: bool) -> Result<bool> {
    if store.companion_work_pending()? {
        return Ok(true);
    }
    if store
        .list_hosts()?
        .iter()
        .any(|host| process_matches(host.pid, host.create_time))
    {
        return Ok(true);
    }
    // A configured standalone companion is itself intentional work. Avoid a
    // restart loop when setup has not supplied credentials yet.
    Ok(standalone && load_credentials()?.is_some())
}

async fn periodic_cleanup(
    store: Arc<Store>,
    logger: ProcessSafeLogger,
    mut stop: watch::Receiver<bool>,
    interval: Duration,
) {
    loop {
        tokio::select! {
            changed = stop.changed() => {
                if changed.is_err() || *stop.borrow() {
                    return;
                }
            }
            _ = tokio::time::sleep(interval) => {
                let cleanup_store = Arc::clone(&store);
                match tokio::task::spawn_blocking(move || cleanup_store.cleanup()).await {
                    Ok(Ok(())) => {}
                    Ok(Err(error)) => {
                        let _ = logger.error(&format!(
                            "Periodic store cleanup failed: {}",
                            safe_detail(&error),
                        ));
                    }
                    Err(error) => {
                        let _ = logger.error(&format!(
                            "Periodic store cleanup task failed: {}",
                            safe_detail(&error),
                        ));
                    }
                }
            }
        }
    }
}

fn prune_dead_hosts(store: &Store) -> Result<usize> {
    let hosts = store.list_hosts()?;
    let dead: Vec<_> = hosts
        .iter()
        .filter(|host| !process_matches(host.pid, host.create_time))
        .cloned()
        .collect();
    if !dead.is_empty() {
        store.remove_hosts(&dead)?;
    }
    Ok(hosts.len().saturating_sub(dead.len()))
}

async fn wait_without_credentials(
    store: Arc<Store>,
    logger: ProcessSafeLogger,
    poll_interval: Duration,
) -> Result<Option<Credentials>> {
    let mut empty_checks = 0u32;
    loop {
        if let Some(credentials) = load_credentials()? {
            let _ = logger.info("QQ credentials became available; starting companion");
            return Ok(Some(credentials));
        }

        let host_store = Arc::clone(&store);
        let live_hosts = tokio::task::spawn_blocking(move || prune_dead_hosts(&host_store))
            .await
            .context("Codex host check task failed")??;
        if live_hosts > 0 {
            empty_checks = 0;
        } else {
            empty_checks += 1;
            if empty_checks >= EMPTY_HOST_CHECKS {
                let _ = logger.info("No Codex host remains; stopping companion");
                return Ok(None);
            }
        }
        tokio::time::sleep(poll_interval).await;
    }
}

async fn run_active_daemon(
    store: Arc<Store>,
    logger: ProcessSafeLogger,
    standalone: bool,
) -> Result<()> {
    let (cleanup_stop, cleanup_receiver) = watch::channel(false);
    let cleanup_task = tokio::spawn(periodic_cleanup(
        Arc::clone(&store),
        logger.clone(),
        cleanup_receiver,
        CLEANUP_INTERVAL,
    ));

    let active_result = async {
        let credentials = match load_credentials()? {
            Some(credentials) => Some(credentials),
            None => {
                let _ =
                    logger.error("QQ credentials are missing; run install.cmd or codexbot setup");
                wait_without_credentials(
                    Arc::clone(&store),
                    logger.clone(),
                    CREDENTIAL_POLL_INTERVAL,
                )
                .await?
            }
        };
        if let Some(credentials) = credentials {
            run_qq_runtime(Arc::clone(&store), credentials, logger.clone(), standalone).await?;
        }
        Ok::<(), anyhow::Error>(())
    }
    .await;

    let _ = cleanup_stop.send(true);
    if let Err(error) = cleanup_task.await {
        let _ = logger.error(&format!(
            "Periodic store cleanup task stopped unexpectedly: {}",
            safe_detail(&error),
        ));
    }
    active_result
}

/// Run the companion daemon. A second process observes the singleton lock and
/// exits successfully without disturbing the active daemon's PID record.
pub async fn run() -> Result<i32> {
    let root = ensure_data_dir().context("failed to prepare CodexBot data directory")?;
    let mut singleton = FileLock::immediate(root.join("daemon.lock"));
    if !singleton
        .acquire()
        .context("failed to acquire daemon lock")?
    {
        return Ok(0);
    }

    let logger = configure_logging("codexbot.daemon", false)
        .context("failed to configure daemon logging")?;
    let store = Arc::new(Store::new(database_path()).context("failed to open state store")?);
    let standalone = env::var("CODEXBOT_STANDALONE").as_deref() == Ok("1");
    if standalone {
        let _ = logger.info("Running as a standalone companion (CODEXBOT_STANDALONE=1)");
    }

    let pid = std::process::id();
    store
        .set_daemon_info(pid, current_process_create_time())
        .context("failed to record daemon process")?;

    let runtime_result = async {
        store.cleanup().context("initial store cleanup failed")?;
        run_active_daemon(Arc::clone(&store), logger.clone(), standalone).await
    }
    .await;
    let mut exit_code = 0;
    if let Err(error) = runtime_result {
        let _ = logger.error(&format!(
            "Companion stopped unexpectedly: {}",
            safe_detail(&error),
        ));
        exit_code = 1;
    }

    if let Err(error) = store.clear_daemon_info(pid) {
        let _ = logger.error(&format!(
            "Failed to clear daemon process record: {}",
            safe_detail(&error),
        ));
        exit_code = 1;
    }
    if let Err(error) = singleton.release() {
        let _ = logger.error(&format!(
            "Failed to release daemon lock: {}",
            safe_detail(&error),
        ));
        exit_code = 1;
    }
    // Drop the file handle before checking/spawning a successor so it can
    // acquire the singleton immediately.
    drop(singleton);

    match lifecycle_work_remains(&store, standalone) {
        Ok(true) => {
            if let Err(error) = ensure_daemon(store.as_ref(), standalone) {
                let _ = logger.error(&format!(
                    "Failed to hand off companion work: {}",
                    safe_detail(&error),
                ));
                exit_code = 1;
            }
        }
        Ok(false) => {}
        Err(error) => {
            let _ = logger.error(&format!(
                "Failed to check remaining companion work: {}",
                safe_detail(&error),
            ));
            exit_code = 1;
        }
    }
    Ok(exit_code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logged_error_details_are_redacted_and_bounded() {
        let detail = safe_detail(&format!("api_key=daemon-secret {}", "x".repeat(500)));
        assert!(!detail.contains("daemon-secret"));
        assert!(detail.contains("[REDACTED]"));
        assert!(detail.chars().count() <= 300);
    }
}
