//! Per-user paths used by CodexBot.
//!
//! The native runtime stores all mutable state below
//! `%LOCALAPPDATA%\CodexBot`. Keeping the path policy in one module also makes
//! tests deterministic through `CODEXBOT_DATA_DIR`.

use std::env;
use std::io;
use std::path::{Component, PathBuf};

pub const APP_DIR_NAME: &str = "CodexBot";

fn home_dir() -> PathBuf {
    env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn expand_user(path: PathBuf) -> PathBuf {
    let value = path.to_string_lossy();
    if value == "~" {
        return home_dir();
    }
    if let Some(rest) = value
        .strip_prefix("~/")
        .or_else(|| value.strip_prefix("~\\"))
    {
        return home_dir().join(rest);
    }
    path
}

/// Lexically normalize an absolute path without requiring the target to
/// exist. This mirrors `Path.resolve()` closely enough for configuration
/// overrides while avoiding surprising failures during first-time setup.
fn absolute_normalized(path: PathBuf) -> PathBuf {
    let path = expand_user(path);
    let absolute = if path.is_absolute() {
        path
    } else {
        env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    if let Ok(canonical) = absolute.canonicalize() {
        return canonical;
    }

    let mut normalized = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    normalized.push(component.as_os_str());
                }
            }
            _ => normalized.push(component.as_os_str()),
        }
    }
    normalized
}

pub fn data_dir() -> PathBuf {
    if let Some(override_path) = env::var_os("CODEXBOT_DATA_DIR").filter(|v| !v.is_empty()) {
        return absolute_normalized(PathBuf::from(override_path));
    }
    if let Some(local_app_data) = env::var_os("LOCALAPPDATA").filter(|v| !v.is_empty()) {
        return PathBuf::from(local_app_data).join(APP_DIR_NAME);
    }
    home_dir().join("AppData").join("Local").join(APP_DIR_NAME)
}

pub fn ensure_data_dir() -> io::Result<PathBuf> {
    let root = data_dir();
    std::fs::create_dir_all(&root)?;
    Ok(root)
}

pub fn database_path() -> PathBuf {
    data_dir().join("state.sqlite3")
}

pub fn bin_dir() -> PathBuf {
    data_dir().join("bin")
}

pub fn installed_executable() -> PathBuf {
    bin_dir().join(if cfg!(windows) {
        "codexbot.exe"
    } else {
        "codexbot"
    })
}

pub fn logs_dir() -> PathBuf {
    data_dir().join("logs")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_binary_matches_the_native_layout() {
        let expected = if cfg!(windows) {
            "bin/codexbot.exe"
        } else {
            "bin/codexbot"
        };
        assert!(installed_executable().ends_with(std::path::Path::new(expected)));
    }
}
