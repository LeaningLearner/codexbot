use std::env;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command as ProcessCommand;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, anyhow, bail};
use chrono::{Local, TimeZone};
use clap::{Parser, Subcommand};
use serde_json::Value;
use sysinfo::{Pid, ProcessesToUpdate, System};

use crate::installer::{
    PERMISSION_NOTIFICATION_ENV, find_codex_command, install_personal_plugin,
    marketplace_contains_plugin,
};
use crate::paths::{database_path, ensure_data_dir, installed_executable};
use crate::processes::{ensure_daemon, process_matches};
use crate::security::{
    Credentials, generate_pairing_code, load_credentials, redact_secrets, store_credentials,
};
use crate::store::Store;
use crate::subprocess_utils::hide_console_window;

const STANDALONE_SETTING: &str = "daemon_standalone";

#[derive(Debug, Parser)]
#[command(name = "codexbot", version, about = "Codex → QQ 官方沙箱通知机器人")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// 保存 QQ 凭据并安装个人 Codex 插件。
    Setup {
        /// CodexBot 源码目录。
        #[arg(long)]
        repo_root: PathBuf,
        /// 替换 Windows Credential Manager 中的现有凭据。
        #[arg(long)]
        replace_credentials: bool,
        /// 仅安装文件，不调用 `codex plugin add`。
        #[arg(long, hide = true)]
        skip_codex_registration: bool,
    },
    /// 生成 30 分钟有效的一次性 QQ 配对码。
    Pair,
    /// 检查安装、凭据、插件、daemon 和 QQ 连接。
    Doctor {
        /// 跳过 QQ 网络认证。
        #[arg(long)]
        offline: bool,
    },
    /// 启动保持 QQ 在线的常驻 daemon。
    Start,
    /// 停止当前 daemon。
    Stop,
    /// 内部 daemon 入口。
    #[command(hide = true)]
    Daemon,
    /// Codex 生命周期 Hook 的中性入口。
    #[command(hide = true)]
    Hook,
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn home_dir() -> PathBuf {
    env::var_os("USERPROFILE")
        .or_else(|| env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn truthy_environment(name: &str) -> bool {
    env::var(name)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        })
        .unwrap_or(false)
}

fn safe_cli_text(value: impl std::fmt::Display, limit: usize) -> String {
    let value = redact_secrets(&value.to_string()).replace(['\r', '\n'], " ");
    let compact = value.split_whitespace().collect::<Vec<_>>().join(" ");
    let bounded: String = compact.chars().take(limit).collect();
    if bounded.is_empty() {
        "外部命令失败".to_owned()
    } else {
        bounded
    }
}

fn create_pairing(store: &Store) -> Result<(String, f64)> {
    let code = generate_pairing_code();
    let expiry = now() + 30.0 * 60.0;
    store.create_pairing(&code, expiry)?;
    Ok((code, expiry))
}

fn command_pair() -> Result<i32> {
    ensure_data_dir()?;
    let store = Store::new(database_path())?;
    let (code, expiry) = create_pairing(&store)?;
    let expiry = Local
        .timestamp_opt(expiry as i64, 0)
        .single()
        .ok_or_else(|| anyhow!("配对码到期时间无效"))?;
    println!("一次性配对码：{code}");
    println!("有效期至：{}", expiry.format("%Y-%m-%d %H:%M:%S"));
    println!("请用沙箱 QQ 私聊机器人发送：/bind {code}");
    Ok(0)
}

fn prompt_credentials() -> Result<()> {
    println!("请输入 QQ 机器人沙箱凭据；AppSecret 输入时不会回显。");
    print!("AppID: ");
    io::stdout().flush()?;
    let mut app_id = String::new();
    io::stdin().read_line(&mut app_id)?;
    let app_secret = rpassword::prompt_password("AppSecret: ")?;
    store_credentials(app_id.trim(), app_secret.trim())?;
    println!("凭据已保存到 Windows Credential Manager。");
    Ok(())
}

fn command_setup(
    repo_root: &Path,
    replace_credentials: bool,
    skip_codex_registration: bool,
) -> Result<i32> {
    ensure_data_dir()?;
    if load_credentials()?.is_some() && !replace_credentials {
        println!("Windows Credential Manager 中已有 QQ 凭据，将继续使用。");
    } else {
        prompt_credentials()?;
    }

    let result = install_personal_plugin(
        repo_root,
        None,
        !skip_codex_registration,
        truthy_environment(PERMISSION_NOTIFICATION_ENV),
    )?;
    println!("插件已安装：{}", result.plugin_path.display());
    println!("个人 marketplace：{}", result.marketplace_path.display());
    if !result.codex_output.is_empty() {
        println!("{}", safe_cli_text(result.codex_output, 300));
    }

    let (code, expiry) = create_pairing(&Store::new(database_path())?)?;
    let expiry = Local
        .timestamp_opt(expiry as i64, 0)
        .single()
        .ok_or_else(|| anyhow!("配对码到期时间无效"))?;
    println!();
    println!("接下来：");
    println!("1. 重启 Codex，在 /hooks 中检查并信任 codexbot Hooks。");
    println!("2. 确认 QQ 已加入机器人沙箱并允许机器人主动发送。");
    println!(
        "3. 在 {} 前私聊发送：/bind {code}",
        expiry.format("%H:%M:%S")
    );
    Ok(0)
}

fn json_reports_plugin(value: &Value) -> bool {
    match value {
        Value::Array(items) => items.iter().any(json_reports_plugin),
        Value::Object(object) => {
            let current = ["name", "id", "ref", "identifier", "plugin", "pluginId"]
                .into_iter()
                .filter_map(|key| object.get(key).and_then(Value::as_str))
                .any(|value| value == "codexbot" || value.starts_with("codexbot@"));
            current || object.values().any(json_reports_plugin)
        }
        Value::String(value) => value == "codexbot" || value.starts_with("codexbot@"),
        _ => false,
    }
}

fn codex_plugin_installed() -> (bool, String) {
    let Some(command) = find_codex_command() else {
        return (false, "找不到 codex/codex.cmd".into());
    };
    let mut process = ProcessCommand::new(&command);
    process.args(["plugin", "list", "--json"]);
    hide_console_window(&mut process);
    match process.output() {
        Ok(output) if output.status.success() => {
            match serde_json::from_slice::<Value>(&output.stdout) {
                Ok(payload) if json_reports_plugin(&payload) => (true, "已安装并启用".into()),
                Ok(_) => (false, "Codex 未报告已安装的 codexbot".into()),
                Err(error) => (false, safe_cli_text(error, 180)),
            }
        }
        Ok(output) => (
            false,
            safe_cli_text(
                String::from_utf8_lossy(if output.stderr.is_empty() {
                    &output.stdout
                } else {
                    &output.stderr
                }),
                180,
            ),
        ),
        Err(error) => (false, safe_cli_text(error, 180)),
    }
}

async fn qq_online_check(credentials: &Credentials) -> Result<()> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(8))
        .build()?;
    let token: Value = client
        .post("https://bots.qq.com/app/getAppAccessToken")
        .json(&serde_json::json!({
            "appId": credentials.app_id,
            "clientSecret": credentials.app_secret,
        }))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let access_token = token
        .get("access_token")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| anyhow!("QQ 沙箱认证未返回 access_token"))?;
    let gateway: Value = client
        .get("https://sandbox.api.sgroup.qq.com/gateway/bot")
        .header("Authorization", format!("QQBot {access_token}"))
        .header("X-Union-Appid", &credentials.app_id)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    if gateway
        .get("url")
        .and_then(Value::as_str)
        .is_none_or(str::is_empty)
    {
        bail!("QQ 沙箱 API 未返回 Gateway URL");
    }
    Ok(())
}

async fn command_doctor(offline: bool) -> Result<i32> {
    let root = ensure_data_dir()?;
    let store = Store::new(database_path())?;
    let mut checks: Vec<(&str, bool, String, bool)> = Vec::new();

    let executable = installed_executable();
    checks.push((
        "原生运行时",
        executable.is_file(),
        executable.display().to_string(),
        true,
    ));
    let credentials = load_credentials()?;
    checks.push((
        "QQ 凭据",
        credentials.is_some(),
        if credentials.is_some() {
            "已存入凭据管理器"
        } else {
            "缺失"
        }
        .into(),
        true,
    ));

    let home = home_dir();
    let plugin_manifest = home.join("plugins/codexbot/.codex-plugin/plugin.json");
    checks.push((
        "个人插件文件",
        plugin_manifest.is_file(),
        plugin_manifest.display().to_string(),
        true,
    ));
    let marketplace = home.join(".agents/plugins/marketplace.json");
    checks.push((
        "Marketplace 条目",
        marketplace_contains_plugin(&marketplace),
        marketplace.display().to_string(),
        true,
    ));
    let (installed, detail) = codex_plugin_installed();
    checks.push(("Codex 插件状态", installed, detail, true));

    let bound = store.get_bound_openid()?;
    checks.push((
        "QQ 单用户绑定",
        bound.is_some(),
        if bound.is_some() {
            "已绑定"
        } else {
            "尚未绑定"
        }
        .into(),
        false,
    ));
    let daemon = store.get_daemon_info()?;
    let daemon_alive = daemon.is_some_and(|(pid, created)| process_matches(pid, created));
    checks.push((
        "伴随进程",
        daemon_alive,
        daemon
            .filter(|_| daemon_alive)
            .map(|(pid, _)| format!("PID {pid}"))
            .unwrap_or_else(|| "当前未运行".into()),
        false,
    ));

    if !offline {
        let (ok, detail) = if let Some(credentials) = credentials.as_ref() {
            match qq_online_check(credentials).await {
                Ok(()) => (true, "沙箱认证与 Gateway 检查成功".to_owned()),
                Err(error) => (false, safe_cli_text(error, 180)),
            }
        } else {
            (false, "未配置凭据".into())
        };
        checks.push(("QQ 沙箱连接", ok, detail, true));
    }

    let mut failed_required = false;
    for (label, ok, detail, required) in checks {
        let marker = if ok {
            "OK"
        } else if required {
            "FAIL"
        } else {
            "WARN"
        };
        println!("[{marker}] {label}: {detail}");
        failed_required |= required && !ok;
    }
    println!("[INFO] 数据目录: {}", root.display());
    println!(
        "[INFO] 常驻模式: {}",
        if store.get_setting(STANDALONE_SETTING)?.as_deref() == Some("1") {
            "是"
        } else {
            "否"
        }
    );
    println!("[INFO] Hook 信任状态需在 Codex /hooks 中人工确认。");
    Ok(i32::from(failed_required))
}

fn command_start() -> Result<i32> {
    ensure_data_dir()?;
    let store = Store::new(database_path())?;
    if let Some((pid, created)) = store.get_daemon_info()? {
        if process_matches(pid, created) {
            let mode = if store.get_setting(STANDALONE_SETTING)?.as_deref() == Some("1") {
                "常驻运行中"
            } else {
                "运行中（跟随 Codex）"
            };
            println!("CodexBot 伴随进程已在运行（PID {pid}，{mode}）。");
            return Ok(0);
        }
    }
    if load_credentials()?.is_none() {
        bail!("未配置 QQ 凭据，请先运行 .\\codexbot.cmd setup 或 install.cmd");
    }
    let launched = ensure_daemon(&store, true)?;
    if launched {
        store.set_setting(STANDALONE_SETTING, "1")?;
        let pid = store
            .get_daemon_info()?
            .map(|item| item.0)
            .unwrap_or_default();
        println!("CodexBot 常驻进程已启动（PID {pid}），QQ 机器人将保持在线。");
        println!("停止请运行：.\\codexbot.cmd stop");
        Ok(0)
    } else {
        bail!("未能启动常驻进程，请查看日志排查")
    }
}

fn command_stop() -> Result<i32> {
    ensure_data_dir()?;
    let store = Store::new(database_path())?;
    let Some((pid, created)) = store.get_daemon_info()? else {
        println!("CodexBot 进程未在运行。");
        store.delete_settings([STANDALONE_SETTING])?;
        return Ok(0);
    };
    if !process_matches(pid, created) {
        println!("CodexBot 进程（PID {pid}）已退出，清理记录。");
        store.clear_daemon_info(pid)?;
        store.delete_settings([STANDALONE_SETTING])?;
        return Ok(0);
    }

    let mut system = System::new_all();
    system.refresh_processes(ProcessesToUpdate::Some(&[Pid::from_u32(pid)]), true);
    if let Some(process) = system.process(Pid::from_u32(pid)) {
        if !process.kill() {
            bail!("无法停止 CodexBot 进程（PID {pid}）");
        }
    }
    for _ in 0..50 {
        if !process_matches(pid, created) {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    store.clear_daemon_info(pid)?;
    store.delete_settings([STANDALONE_SETTING])?;
    println!("CodexBot 进程（PID {pid}）已停止，QQ 机器人已下线。");
    Ok(0)
}

fn command_hook() -> i32 {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let _ = crate::hooks::run_from(stdin.lock(), stdout.lock());
    0
}

pub async fn run() -> Result<i32> {
    let cli = Cli::parse();
    match cli.command {
        Command::Setup {
            repo_root,
            replace_credentials,
            skip_codex_registration,
        } => command_setup(&repo_root, replace_credentials, skip_codex_registration),
        Command::Pair => command_pair(),
        Command::Doctor { offline } => command_doctor(offline).await,
        Command::Start => command_start(),
        Command::Stop => command_stop(),
        Command::Daemon => crate::daemon::run().await,
        Command::Hook => Ok(command_hook()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    #[test]
    fn command_line_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn external_text_is_redacted_and_single_line() {
        let value = safe_cli_text("failed\napi_key=secret-value", 300);
        assert!(!value.contains("secret-value"));
        assert!(!value.contains('\n'));
        assert!(value.contains("[REDACTED]"));
    }
}
