//! Codex host discovery and native companion process lifecycle.

use fs2::FileExt;
use std::collections::HashSet;
use std::ffi::OsStr;
use std::fs::OpenOptions;
use std::io;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use sysinfo::{Pid, ProcessesToUpdate, System};
use thiserror::Error;

#[derive(Debug, Clone, PartialEq)]
pub struct ProcessInfo {
    pub pid: u32,
    pub create_time: f64,
    pub name: String,
    pub executable: String,
    pub command_line: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct HostProcess {
    pub pid: u32,
    pub create_time: f64,
    pub kind: String,
}

impl HostProcess {
    pub fn new(pid: u32, create_time: f64, kind: impl Into<String>) -> Self {
        Self {
            pid,
            create_time,
            kind: kind.into(),
        }
    }
}

#[derive(Debug, Error)]
pub enum ProcessError {
    #[error("无法读取 companion 状态：{0}")]
    State(String),
    #[error("无法准备 companion 数据目录：{0}")]
    DataDirectory(#[source] io::Error),
    #[error("无法启动 CodexBot companion：{0}")]
    Spawn(#[source] io::Error),
}

pub trait DaemonState {
    type Error: std::error::Error + Send + Sync + 'static;

    fn get_daemon_info(&self) -> Result<Option<(u32, f64)>, Self::Error>;
    fn set_daemon_info(&self, pid: u32, create_time: f64) -> Result<(), Self::Error>;
}

fn windows_basename(value: &str) -> &str {
    value.rsplit(['\\', '/']).next().unwrap_or(value)
}

fn process_stem(value: &str) -> String {
    let name = windows_basename(value).to_lowercase();
    for suffix in [".exe", ".cmd", ".bat", ".com", ".js"] {
        if let Some(stem) = name.strip_suffix(suffix) {
            return stem.to_owned();
        }
    }
    name
}

fn looks_like_codex_name(value: &str) -> bool {
    let stem = process_stem(value);
    stem == "codex" || stem.starts_with("codex-") || stem.starts_with("codex_")
}

fn looks_like_codex_process(item: &ProcessInfo) -> bool {
    looks_like_codex_name(&item.name)
        || looks_like_codex_name(&item.executable)
        || item
            .command_line
            .iter()
            .take(3)
            .any(|argument| looks_like_codex_name(argument))
}

fn has_app_server_argument(item: &ProcessInfo) -> bool {
    item.command_line.iter().any(|argument| {
        let normalized = windows_basename(argument)
            .to_lowercase()
            .trim_start_matches(['-', '/', '\\'])
            .split('=')
            .next()
            .unwrap_or_default()
            .to_owned();
        matches!(normalized.as_str(), "app-server" | "app_server")
    })
}

fn is_app_server(item: &ProcessInfo) -> bool {
    looks_like_codex_process(item) && has_app_server_argument(item)
}

fn looks_like_desktop_name(value: &str) -> bool {
    let stem = process_stem(value);
    stem == "chatgpt"
        || stem.starts_with("chatgpt-")
        || stem.starts_with("chatgpt_")
        || matches!(
            stem.as_str(),
            "codexdesktop" | "codex-desktop" | "codex_desktop"
        )
        || (stem.contains("codex") && stem.contains("desktop"))
}

fn is_desktop_host(item: &ProcessInfo) -> bool {
    looks_like_desktop_name(&item.name)
        || looks_like_desktop_name(&item.executable)
        || item.command_line.iter().any(|argument| {
            matches!(
                process_stem(argument).as_str(),
                "chatgpt" | "codexdesktop" | "codex-desktop" | "codex_desktop"
            )
        })
}

fn is_transient_codex_helper(item: &ProcessInfo) -> bool {
    const MARKERS: [&str; 4] = ["command-runner", "code-mode-host", "sandbox", "apply-patch"];
    [&item.name, &item.executable]
        .into_iter()
        .map(|value| process_stem(value))
        .chain(item.command_line.iter().map(|value| value.to_lowercase()))
        .any(|value| MARKERS.iter().any(|marker| value.contains(marker)))
}

fn is_transient_desktop_helper(item: &ProcessInfo) -> bool {
    const MARKERS: [&str; 3] = ["--type=", "crashpad", "notification-helper"];
    item.command_line.iter().any(|argument| {
        let argument = argument.to_lowercase();
        MARKERS.iter().any(|marker| argument.contains(marker))
    })
}

pub fn select_codex_host(chain: &[ProcessInfo]) -> Option<HostProcess> {
    if let Some((index, app_server)) = chain
        .iter()
        .enumerate()
        .find(|(_, item)| is_app_server(item))
    {
        if let Some(desktop) = chain[index + 1..]
            .iter()
            .find(|item| is_desktop_host(item) && !is_transient_desktop_helper(item))
        {
            return Some(HostProcess::new(
                desktop.pid,
                desktop.create_time,
                "desktop",
            ));
        }
        return Some(HostProcess::new(
            app_server.pid,
            app_server.create_time,
            "app-server",
        ));
    }

    if let Some(desktop) = chain
        .iter()
        .find(|item| is_desktop_host(item) && !is_transient_desktop_helper(item))
    {
        return Some(HostProcess::new(
            desktop.pid,
            desktop.create_time,
            "desktop",
        ));
    }

    chain
        .iter()
        .find(|item| looks_like_codex_process(item) && !is_transient_codex_helper(item))
        .map(|item| HostProcess::new(item.pid, item.create_time, "cli"))
}

fn os_lossy(value: &OsStr) -> String {
    value.to_string_lossy().into_owned()
}

fn snapshot(process: &sysinfo::Process) -> ProcessInfo {
    ProcessInfo {
        pid: process.pid().as_u32(),
        create_time: process.start_time() as f64,
        name: os_lossy(process.name()),
        executable: process
            .exe()
            .map(Path::to_string_lossy)
            .map(|value| value.into_owned())
            .unwrap_or_default(),
        command_line: process.cmd().iter().map(|value| os_lossy(value)).collect(),
    }
}

fn discovery_candidate(item: &ProcessInfo) -> bool {
    let stem = process_stem(&item.name);
    looks_like_codex_name(&item.name)
        || looks_like_desktop_name(&item.name)
        || matches!(stem.as_str(), "node" | "nodejs")
}

pub fn discover_running_codex_host() -> Option<HostProcess> {
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::All, true);
    let snapshots: Vec<_> = system
        .processes()
        .values()
        .map(snapshot)
        .filter(discovery_candidate)
        .collect();

    if let Some(desktop) = snapshots
        .iter()
        .filter(|item| is_desktop_host(item) && !is_transient_desktop_helper(item))
        .min_by(|left, right| left.create_time.total_cmp(&right.create_time))
    {
        return Some(HostProcess::new(
            desktop.pid,
            desktop.create_time,
            "desktop",
        ));
    }
    if let Some(app_server) = snapshots.iter().find(|item| is_app_server(item)) {
        return Some(HostProcess::new(
            app_server.pid,
            app_server.create_time,
            "app-server",
        ));
    }
    snapshots
        .iter()
        .find(|item| looks_like_codex_process(item) && !is_transient_codex_helper(item))
        .map(|item| HostProcess::new(item.pid, item.create_time, "cli"))
}

pub fn discover_codex_host(start_pid: Option<u32>, ancestor_pids: &[u32]) -> Option<HostProcess> {
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::All, true);

    let mut seen = HashSet::new();
    let ancestor_chain: Vec<_> = ancestor_pids
        .iter()
        .copied()
        .filter(|pid| *pid > 0 && seen.insert(*pid))
        .filter_map(|pid| system.process(Pid::from_u32(pid)).map(snapshot))
        .collect();
    if let Some(host) = select_codex_host(&ancestor_chain) {
        return Some(host);
    }

    let mut chain = Vec::new();
    let mut current = Some(Pid::from_u32(start_pid.unwrap_or_else(std::process::id)));
    let mut seen = HashSet::new();
    while let Some(pid) = current.filter(|pid| seen.insert(pid.as_u32())) {
        let Some(process) = system.process(pid) else {
            break;
        };
        chain.push(snapshot(process));
        current = process.parent();
    }
    select_codex_host(&chain).or_else(discover_running_codex_host)
}

pub fn process_matches(pid: u32, create_time: f64) -> bool {
    process_matches_with_tolerance(pid, create_time, 0.25)
}

pub fn process_matches_with_tolerance(pid: u32, create_time: f64, tolerance: f64) -> bool {
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::Some(&[Pid::from_u32(pid)]), true);
    system
        .process(Pid::from_u32(pid))
        .map(|process| ((process.start_time() as f64) - create_time).abs() <= tolerance)
        .unwrap_or(false)
}

fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn process_create_time(pid: u32) -> Option<f64> {
    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::Some(&[Pid::from_u32(pid)]), true);
    system
        .process(Pid::from_u32(pid))
        .map(|process| process.start_time() as f64)
}

pub fn ensure_daemon<S: DaemonState>(state: &S, standalone: bool) -> Result<bool, ProcessError> {
    let info = state
        .get_daemon_info()
        .map_err(|error| ProcessError::State(error.to_string()))?;
    if info.is_some_and(|(pid, created)| process_matches(pid, created)) {
        return Ok(false);
    }

    let data_dir = crate::paths::data_dir();
    std::fs::create_dir_all(&data_dir).map_err(ProcessError::DataDirectory)?;
    let lock_path = data_dir.join("daemon-start.lock");
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)
        .map_err(ProcessError::DataDirectory)?;
    if lock.try_lock_exclusive().is_err() {
        return Ok(false);
    }

    let result = (|| {
        let info = state
            .get_daemon_info()
            .map_err(|error| ProcessError::State(error.to_string()))?;
        if info.is_some_and(|(pid, created)| process_matches(pid, created)) {
            return Ok(false);
        }

        let executable = std::env::current_exe().map_err(ProcessError::Spawn)?;
        let mut command = Command::new(executable);
        command
            .arg("daemon")
            .current_dir(&data_dir)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .env("CODEXBOT_DATA_DIR", &data_dir)
            .env("CODEXBOT_STANDALONE", if standalone { "1" } else { "0" });

        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::{CREATE_NO_WINDOW, DETACHED_PROCESS};
            command.creation_flags(CREATE_NO_WINDOW | DETACHED_PROCESS);
        }

        let child = command.spawn().map_err(ProcessError::Spawn)?;
        let pid = child.id();
        let create_time = (0..3)
            .find_map(|_| {
                let value = process_create_time(pid);
                if value.is_none() {
                    std::thread::sleep(Duration::from_millis(10));
                }
                value
            })
            .unwrap_or_else(unix_now);
        state
            .set_daemon_info(pid, create_time)
            .map_err(|error| ProcessError::State(error.to_string()))?;
        Ok(true)
    })();
    let _ = FileExt::unlock(&lock);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn process(pid: u32, name: &str, executable: &str, command_line: &[&str]) -> ProcessInfo {
        ProcessInfo {
            pid,
            create_time: pid as f64,
            name: name.to_owned(),
            executable: executable.to_owned(),
            command_line: command_line
                .iter()
                .map(|value| (*value).to_owned())
                .collect(),
        }
    }

    #[test]
    fn desktop_wins_over_nested_app_server() {
        let chain = vec![
            process(10, "python.exe", r"C:\Python\python.exe", &["python"]),
            process(
                20,
                "codex.exe",
                r"C:\Codex\codex.exe",
                &["codex.exe", "app-server"],
            ),
            process(
                30,
                "ChatGPT.exe",
                r"C:\WindowsApps\OpenAI.Codex_x64\ChatGPT.exe",
                &["ChatGPT.exe"],
            ),
        ];
        assert_eq!(
            select_codex_host(&chain),
            Some(HostProcess::new(30, 30.0, "desktop"))
        );
    }

    #[test]
    fn skips_transient_runner() {
        let runner = process(
            10,
            "codex-command-runner.exe",
            r"C:\Codex\codex-command-runner.exe",
            &["codex-command-runner.exe"],
        );
        let cli = process(
            20,
            "codex.exe",
            r"C:\Codex\codex.exe",
            &["codex.exe", "exec"],
        );
        assert_eq!(
            select_codex_host(&[runner, cli]),
            Some(HostProcess::new(20, 20.0, "cli"))
        );
    }
}
