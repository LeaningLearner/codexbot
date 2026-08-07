//! Helpers for starting Codex child processes without a console window.

use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

pub const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
pub const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct HiddenConsoleOptions {
    pub creation_flags: u32,
}

pub fn hidden_console_options(new_process_group: bool) -> HiddenConsoleOptions {
    if !cfg!(windows) {
        return HiddenConsoleOptions::default();
    }
    let mut creation_flags = CREATE_NO_WINDOW;
    if new_process_group {
        creation_flags |= CREATE_NEW_PROCESS_GROUP;
    }
    HiddenConsoleOptions { creation_flags }
}

/// Apply Windows' `CREATE_NO_WINDOW` flag to a command. On other platforms it
/// is deliberately a no-op so callers can share one launch path.
pub fn apply_hidden_console(command: &mut Command, new_process_group: bool) -> &mut Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(hidden_console_options(new_process_group).creation_flags);
    }
    #[cfg(not(windows))]
    {
        let _ = new_process_group;
    }
    command
}

/// Backwards-compatible convenience used by short-lived CLI invocations.
pub fn hide_console_window(command: &mut Command) -> &mut Command {
    apply_hidden_console(command, false)
}

pub fn npm_codex_native_executable(shim: impl AsRef<Path>) -> Option<PathBuf> {
    npm_codex_native_executable_for(shim.as_ref(), cfg!(windows), std::env::consts::ARCH)
}

fn executable_on_path(name: &str) -> Option<PathBuf> {
    let candidate = Path::new(name);
    if candidate.components().count() > 1 {
        return candidate.is_file().then(|| candidate.to_path_buf());
    }
    let path = env::var_os("PATH")?;
    env::split_paths(&path)
        .map(|directory| directory.join(candidate))
        .find(|candidate| candidate.is_file())
}

fn resolve_codex_executable_for(program: &Path, windows: bool, machine: &str) -> Option<PathBuf> {
    if !program.is_file() {
        return None;
    }
    if !windows {
        return Some(program.to_path_buf());
    }
    match program
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("cmd") => npm_codex_native_executable_for(program, true, machine),
        Some("exe") => Some(program.to_path_buf()),
        _ => None,
    }
}

/// Locate an executable Codex program. On Windows this deliberately resolves
/// the npm `codex.cmd` shim to its bundled native executable because
/// `std::process::Command` cannot execute batch files directly.
pub fn find_codex_executable() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        if let Some(shim) = executable_on_path("codex.cmd")
            && let Some(native) = resolve_codex_executable_for(&shim, true, std::env::consts::ARCH)
        {
            return Some(native);
        }
        executable_on_path("codex.exe").and_then(|program| {
            resolve_codex_executable_for(&program, true, std::env::consts::ARCH)
        })
    }
    #[cfg(not(windows))]
    {
        executable_on_path("codex")
    }
}

/// Resolve an explicit Codex command override without accepting a Windows
/// script that `Command` would later reject with `ERROR_BAD_EXE_FORMAT`.
pub fn resolve_codex_executable(program: impl AsRef<Path>) -> Option<PathBuf> {
    let program = program.as_ref();
    #[cfg(windows)]
    if program.components().count() == 1
        && program
            .file_stem()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.eq_ignore_ascii_case("codex"))
    {
        return find_codex_executable();
    }
    let located = executable_on_path(program.to_str()?)?;
    resolve_codex_executable_for(&located, cfg!(windows), std::env::consts::ARCH)
}

/// Testable resolver for the optional native binary shipped behind the npm
/// `codex.cmd` shim. It intentionally never searches PATH and therefore cannot
/// select the access-restricted WindowsApps resource.
pub fn npm_codex_native_executable_for(
    shim: &Path,
    windows: bool,
    machine: &str,
) -> Option<PathBuf> {
    if !windows || shim.as_os_str().is_empty() {
        return None;
    }
    let machine = machine.to_ascii_lowercase();
    let (package_name, target) = if matches!(machine.as_str(), "arm64" | "aarch64") {
        ("codex-win32-arm64", "aarch64-pc-windows-msvc")
    } else {
        ("codex-win32-x64", "x86_64-pc-windows-msvc")
    };

    let npm_root = shim.parent()?;
    let package_root = npm_root.join("node_modules").join("@openai").join("codex");
    let candidates = [
        package_root
            .join("node_modules")
            .join("@openai")
            .join(package_name)
            .join("vendor")
            .join(target)
            .join("bin")
            .join("codex.exe"),
        npm_root
            .join("node_modules")
            .join("@openai")
            .join(package_name)
            .join("vendor")
            .join(target)
            .join("bin")
            .join("codex.exe"),
        package_root
            .join("vendor")
            .join(target)
            .join("bin")
            .join("codex.exe"),
    ];
    candidates.into_iter().find(|candidate| candidate.is_file())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_binary_is_resolved_only_beside_the_npm_shim() {
        let root = std::env::temp_dir().join(format!("codexbot-npm-test-{}", std::process::id()));
        let shim = root.join("bin").join("codex.cmd");
        let native = shim
            .parent()
            .unwrap()
            .join("node_modules/@openai/codex/node_modules/@openai/codex-win32-x64")
            .join("vendor/x86_64-pc-windows-msvc/bin/codex.exe");
        std::fs::create_dir_all(native.parent().unwrap()).unwrap();
        std::fs::write(&shim, b"@echo off").unwrap();
        std::fs::write(&native, b"MZ").unwrap();
        assert_eq!(
            npm_codex_native_executable_for(&shim, true, "AMD64"),
            Some(native.clone())
        );
        assert_eq!(npm_codex_native_executable_for(&shim, false, "AMD64"), None);
        assert_eq!(
            resolve_codex_executable_for(&shim, true, "AMD64"),
            Some(native.clone())
        );
        assert_eq!(
            resolve_codex_executable_for(&native, true, "AMD64"),
            Some(native)
        );
        let _ = std::fs::remove_dir_all(root);
    }
}
