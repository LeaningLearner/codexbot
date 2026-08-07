//! Bounded process-safe file logging shared by hook and daemon processes.

use crate::locks::FileLock;
use crate::paths::logs_dir;
use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const LOG_MAX_BYTES: u64 = 1_000_000;
pub const LOG_BACKUP_COUNT: usize = 3;
const TRUNCATION_MARKER: &[u8] = b"... [log record truncated]\n";

fn thread_locks() -> &'static Mutex<HashMap<PathBuf, Arc<Mutex<()>>>> {
    static LOCKS: OnceLock<Mutex<HashMap<PathBuf, Arc<Mutex<()>>>>> = OnceLock::new();
    LOCKS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn thread_lock(path: &Path) -> Arc<Mutex<()>> {
    let mut locks = thread_locks()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    locks
        .entry(path.to_path_buf())
        .or_insert_with(|| Arc::new(Mutex::new(())))
        .clone()
}

#[derive(Debug, Clone)]
pub struct ProcessSafeRotatingFileHandler {
    path: PathBuf,
    max_bytes: u64,
    backup_count: usize,
    lock_timeout: Duration,
}

impl ProcessSafeRotatingFileHandler {
    pub fn new(path: impl Into<PathBuf>, max_bytes: u64, backup_count: usize) -> Self {
        let path = path.into();
        let path = if path.is_absolute() {
            path
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("."))
                .join(path)
        };
        Self {
            path,
            max_bytes: max_bytes.max(1),
            backup_count,
            lock_timeout: Duration::from_secs(1),
        }
    }

    pub fn with_lock_timeout(mut self, timeout: Duration) -> Self {
        self.lock_timeout = timeout;
        self
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn lock_path(&self) -> PathBuf {
        let name = self
            .path
            .file_name()
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_else(|| "codexbot.log".to_owned());
        self.path.with_file_name(format!("{name}.lock"))
    }

    fn backup_path(&self, index: usize) -> PathBuf {
        let name = self
            .path
            .file_name()
            .map(|value| value.to_string_lossy().into_owned())
            .unwrap_or_else(|| "codexbot.log".to_owned());
        self.path.with_file_name(format!("{name}.{index}"))
    }

    fn rotate(&self) -> io::Result<()> {
        if self.backup_count == 0 {
            match fs::remove_file(&self.path) {
                Ok(()) => return Ok(()),
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(error),
            }
        }

        for index in (1..=self.backup_count).rev() {
            let target = self.backup_path(index);
            let source = if index == 1 {
                self.path.clone()
            } else {
                self.backup_path(index - 1)
            };
            match fs::remove_file(&target) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
            if source.exists() {
                fs::rename(source, target)?;
            }
        }
        Ok(())
    }

    fn record_bytes(&self, record: &str) -> Vec<u8> {
        let mut data = record.as_bytes().to_vec();
        data.push(b'\n');
        let maximum = self.max_bytes as usize;
        if data.len() <= maximum {
            return data;
        }
        if TRUNCATION_MARKER.len() >= maximum {
            return TRUNCATION_MARKER[..maximum].to_vec();
        }

        let prefix_limit = maximum - TRUNCATION_MARKER.len();
        let mut boundary = prefix_limit.min(data.len());
        while boundary > 0 && !record.is_char_boundary(boundary.min(record.len())) {
            boundary -= 1;
        }
        let mut shortened = data[..boundary].to_vec();
        shortened.extend_from_slice(TRUNCATION_MARKER);
        shortened.truncate(maximum);
        shortened
    }

    /// Write one already-formatted record. `Ok(false)` means another process
    /// held the lock beyond the configured timeout and the best-effort record
    /// was intentionally dropped.
    pub fn emit(&self, record: &str) -> io::Result<bool> {
        let data = self.record_bytes(record);
        let local_lock = thread_lock(&self.path);
        let _local_guard = local_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut process_lock = FileLock::new(self.lock_path(), self.lock_timeout);
        if !process_lock.acquire()? {
            return Ok(false);
        }

        let result = (|| {
            if let Some(parent) = self.path.parent() {
                fs::create_dir_all(parent)?;
            }
            let current_size = match fs::metadata(&self.path) {
                Ok(metadata) => metadata.len(),
                Err(error) if error.kind() == io::ErrorKind::NotFound => 0,
                Err(error) => return Err(error),
            };
            if current_size > 0 && current_size.saturating_add(data.len() as u64) > self.max_bytes {
                self.rotate()?;
            }
            let mut handle = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&self.path)?;
            handle.write_all(&data)?;
            handle.flush()
        })();
        let release_result = process_lock.release();
        result.and(release_result).map(|()| true)
    }
}

#[derive(Debug, Clone)]
pub struct ProcessSafeLogger {
    name: String,
    verbose: bool,
    handler: ProcessSafeRotatingFileHandler,
}

impl ProcessSafeLogger {
    fn timestamp() -> String {
        let elapsed = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or(Duration::ZERO);
        format!("{}.{:03}", elapsed.as_secs(), elapsed.subsec_millis())
    }

    pub fn log(&self, level: &str, message: &str) -> io::Result<bool> {
        self.handler.emit(&format!(
            "{} {} {} {}",
            Self::timestamp(),
            level,
            self.name,
            message
        ))
    }

    pub fn debug(&self, message: &str) -> io::Result<bool> {
        if !self.verbose {
            return Ok(false);
        }
        self.log("DEBUG", message)
    }

    pub fn info(&self, message: &str) -> io::Result<bool> {
        self.log("INFO", message)
    }

    pub fn warning(&self, message: &str) -> io::Result<bool> {
        self.log("WARNING", message)
    }

    pub fn error(&self, message: &str) -> io::Result<bool> {
        self.log("ERROR", message)
    }

    pub fn handler(&self) -> &ProcessSafeRotatingFileHandler {
        &self.handler
    }
}

pub fn configure_logging(name: &str, verbose: bool) -> io::Result<ProcessSafeLogger> {
    let directory = logs_dir();
    fs::create_dir_all(&directory)?;
    Ok(ProcessSafeLogger {
        name: name.to_owned(),
        verbose,
        handler: ProcessSafeRotatingFileHandler::new(
            directory.join("codexbot.log"),
            LOG_MAX_BYTES,
            LOG_BACKUP_COUNT,
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn files_and_backups_remain_bounded() {
        let root = std::env::temp_dir().join(format!("codexbot-log-test-{}", std::process::id()));
        let path = root.join("codexbot.log");
        let handler = ProcessSafeRotatingFileHandler::new(&path, 128, 2);
        for _ in 0..30 {
            handler.emit(&"x".repeat(80)).unwrap();
        }
        for candidate in [&path, &handler.backup_path(1), &handler.backup_path(2)] {
            if candidate.exists() {
                assert!(candidate.metadata().unwrap().len() <= 128);
            }
        }
        assert!(!handler.backup_path(3).exists());
        let _ = fs::remove_dir_all(root);
    }
}
