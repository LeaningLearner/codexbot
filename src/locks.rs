//! Small cross-platform advisory file lock for per-user singleton processes.

use fs2::FileExt;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct FileLock {
    path: PathBuf,
    timeout: Duration,
    handle: Option<File>,
}

impl FileLock {
    pub fn new(path: impl Into<PathBuf>, timeout: Duration) -> Self {
        Self {
            path: path.into(),
            timeout,
            handle: None,
        }
    }

    pub fn immediate(path: impl Into<PathBuf>) -> Self {
        Self::new(path, Duration::ZERO)
    }

    pub fn with_timeout_secs(path: impl Into<PathBuf>, seconds: f64) -> Self {
        let timeout = if seconds.is_finite() && seconds > 0.0 {
            Duration::from_secs_f64(seconds)
        } else {
            Duration::ZERO
        };
        Self::new(path, timeout)
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn is_acquired(&self) -> bool {
        self.handle.is_some()
    }

    pub fn acquire(&mut self) -> io::Result<bool> {
        if self.handle.is_some() {
            return Ok(true);
        }
        if let Some(parent) = self.path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut handle = OpenOptions::new()
            .read(true)
            .append(true)
            .create(true)
            .open(&self.path)?;
        if handle.metadata()?.len() == 0 {
            handle.write_all(&[0])?;
            handle.flush()?;
        }

        let started = Instant::now();
        loop {
            match handle.try_lock_exclusive() {
                Ok(()) => {
                    self.handle = Some(handle);
                    return Ok(true);
                }
                Err(_) if started.elapsed() >= self.timeout => return Ok(false),
                Err(_) => {
                    let remaining = self.timeout.saturating_sub(started.elapsed());
                    thread::sleep(remaining.min(Duration::from_millis(50)));
                }
            }
        }
    }

    pub fn release(&mut self) -> io::Result<()> {
        let Some(handle) = self.handle.take() else {
            return Ok(());
        };
        FileExt::unlock(&handle)
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = self.release();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_second_lock_waits_until_the_first_is_released() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("single.lock");
        let mut first = FileLock::immediate(&path);
        let mut second = FileLock::immediate(&path);
        assert!(first.acquire().unwrap());
        assert!(!second.acquire().unwrap());
        first.release().unwrap();
        assert!(second.acquire().unwrap());
        second.release().unwrap();
    }
}
