use std::collections::BTreeSet;
use std::env;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use anyhow::{Context, Result, anyhow, bail};
use chrono::Utc;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use crate::subprocess_utils::hide_console_window;

pub const PLUGIN_NAME: &str = "codexbot";
pub const CORE_HOOK_EVENTS: [&str; 4] = ["SessionEnd", "SessionStart", "Stop", "UserPromptSubmit"];
pub const PERMISSION_HOOK_EVENTS: [&str; 2] = ["PermissionRequest", "PostToolUse"];
pub const PERMISSION_NOTIFICATION_ENV: &str = "CODEXBOT_NOTIFY_PERMISSION_REQUESTS";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstallResult {
    pub plugin_path: PathBuf,
    pub marketplace_path: PathBuf,
    pub marketplace_name: String,
    pub codex_output: String,
}

fn read_json(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path).with_context(|| format!("无法读取 {}", path.display()))?;
    let value: Value =
        serde_json::from_str(&text).with_context(|| format!("无法解析 {}", path.display()))?;
    if !value.is_object() {
        bail!("{} 的根节点必须是 JSON 对象", path.display());
    }
    Ok(value)
}

fn write_atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| anyhow!("路径没有父目录：{}", path.display()))?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        Uuid::new_v4()
    ));
    fs::write(&temporary, bytes)?;
    if let Err(error) = fs::rename(&temporary, path) {
        if path.exists() {
            fs::remove_file(path)?;
            fs::rename(&temporary, path)?;
        } else {
            let _ = fs::remove_file(&temporary);
            return Err(error.into());
        }
    }
    Ok(())
}

fn write_json(path: &Path, value: &Value, backup: bool) -> Result<()> {
    if backup && path.is_file() {
        let backup_path = path.with_extension(format!(
            "{}.codexbot.bak",
            path.extension().unwrap_or_default().to_string_lossy()
        ));
        fs::copy(path, backup_path)?;
    }
    let mut bytes = serde_json::to_vec_pretty(value)?;
    bytes.push(b'\n');
    write_atomic(path, &bytes)
}

fn hook_names(document: &Value) -> Result<BTreeSet<String>> {
    let hooks = document
        .get("hooks")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("hooks.json 缺少 hooks 对象"))?;
    Ok(hooks.keys().cloned().collect())
}

pub fn validate_plugin_tree(plugin_path: &Path) -> Result<()> {
    let manifest_path = plugin_path.join(".codex-plugin").join("plugin.json");
    let hooks_path = plugin_path.join("hooks").join("hooks.json");
    if !manifest_path.is_file() || !hooks_path.is_file() {
        bail!("插件结构不完整：{}", plugin_path.display());
    }

    let manifest = read_json(&manifest_path)?;
    if manifest.get("name").and_then(Value::as_str) != Some(PLUGIN_NAME)
        || plugin_path.file_name().and_then(|name| name.to_str()) != Some(PLUGIN_NAME)
    {
        bail!("插件目录名与 manifest name 必须都是 codexbot");
    }
    for field in ["version", "description"] {
        if manifest
            .get(field)
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or_default()
            .is_empty()
        {
            bail!("plugin.json 缺少必填字段：{field}");
        }
    }
    let interface = manifest
        .get("interface")
        .and_then(Value::as_object)
        .ok_or_else(|| anyhow!("plugin.json.interface 必须是对象"))?;
    for field in ["displayName", "shortDescription", "defaultPrompt"] {
        if interface
            .get(field)
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or_default()
            .is_empty()
        {
            bail!("plugin.json.interface 缺少必填字段：{field}");
        }
    }
    if manifest.get("hooks").is_some() {
        bail!("plugin.json 不支持 hooks 字段；应使用 hooks/hooks.json 自动发现");
    }

    let document = read_json(&hooks_path)?;
    let names = hook_names(&document)?;
    let core: BTreeSet<_> = CORE_HOOK_EVENTS
        .iter()
        .map(|item| item.to_string())
        .collect();
    let all: BTreeSet<_> = CORE_HOOK_EVENTS
        .iter()
        .chain(PERMISSION_HOOK_EVENTS.iter())
        .map(|item| item.to_string())
        .collect();
    if names != core && names != all {
        bail!("hooks.json 的 CodexBot 生命周期事件集合无效");
    }

    let hooks = document["hooks"].as_object().expect("validated above");
    for (event, groups) in hooks {
        let groups = groups
            .as_array()
            .filter(|groups| !groups.is_empty())
            .ok_or_else(|| anyhow!("Hook 未配置：{event}"))?;
        let handlers = groups[0]
            .get("hooks")
            .and_then(Value::as_array)
            .filter(|handlers| !handlers.is_empty())
            .ok_or_else(|| anyhow!("Hook 没有命令处理器：{event}"))?;
        let handler = handlers[0]
            .as_object()
            .ok_or_else(|| anyhow!("Hook 处理器无效：{event}"))?;
        if handler.get("type").and_then(Value::as_str) != Some("command") {
            bail!("Hook 处理器类型无效：{event}");
        }
        for field in ["command", "commandWindows"] {
            if handler
                .get(field)
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or_default()
                .is_empty()
            {
                bail!("Hook {field} 不能为空：{event}");
            }
        }
        let timeout = handler
            .get("timeout")
            .and_then(Value::as_f64)
            .ok_or_else(|| anyhow!("Hook timeout 无效：{event}"))?;
        if !(0.0 < timeout && timeout <= 2.0) {
            bail!("Hook timeout 必须在 0-2 秒内：{event}");
        }
    }
    Ok(())
}

fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir_all(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let name = entry.file_name();
        let name_text = name.to_string_lossy();
        if name_text == "__pycache__" || name_text.ends_with(".pyc") || name_text.ends_with(".pyo")
        {
            continue;
        }
        let target = destination.join(&name);
        if entry.file_type()?.is_dir() {
            copy_tree(&entry.path(), &target)?;
        } else {
            fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

fn cachebust_manifest(plugin_path: &Path) -> Result<()> {
    let path = plugin_path.join(".codex-plugin").join("plugin.json");
    let mut manifest = read_json(&path)?;
    let object = manifest.as_object_mut().expect("validated JSON object");
    let base = object
        .get("version")
        .and_then(Value::as_str)
        .unwrap_or("0.1.0")
        .split('+')
        .next()
        .unwrap_or("0.1.0");
    let stamp = Utc::now().format("local-%Y%m%d-%H%M%S-%6f");
    object.insert(
        "version".into(),
        Value::String(format!("{base}+codex.{stamp}")),
    );
    write_json(&path, &manifest, false)
}

fn materialize_hooks(
    staged_plugin: &Path,
    final_plugin: &Path,
    permission_notifications: bool,
) -> Result<()> {
    let path = staged_plugin.join("hooks").join("hooks.json");
    let mut document = read_json(&path)?;
    let hooks = document
        .get_mut("hooks")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| anyhow!("hooks.json 缺少 hooks 对象"))?;
    if !permission_notifications {
        for event in PERMISSION_HOOK_EVENTS {
            hooks.remove(event);
        }
    }

    let entry = final_plugin.join("hooks").join("entry.cmd");
    let command = format!("cmd.exe /D /S /C \"\"{}\"\"", entry.display());
    for (event, groups) in hooks {
        let groups = groups
            .as_array_mut()
            .ok_or_else(|| anyhow!("Hook 配置无效：{event}"))?;
        for group in groups {
            let handlers = group
                .get_mut("hooks")
                .and_then(Value::as_array_mut)
                .ok_or_else(|| anyhow!("Hook 没有命令处理器：{event}"))?;
            for handler in handlers {
                let handler = handler
                    .as_object_mut()
                    .ok_or_else(|| anyhow!("Hook 处理器无效：{event}"))?;
                handler.insert("command".into(), Value::String(command.clone()));
                handler.insert("commandWindows".into(), Value::String(command.clone()));
            }
        }
    }
    write_json(&path, &document, false)
}

fn marketplace_backup(path: &Path) -> PathBuf {
    path.with_extension(format!(
        "{}.codexbot.bak",
        path.extension().unwrap_or_default().to_string_lossy()
    ))
}

fn merge_marketplace(path: &Path) -> Result<String> {
    let mut marketplace = if path.exists() {
        read_json(path)?
    } else {
        json!({
            "name": "personal",
            "interface": {"displayName": "Personal"},
            "plugins": []
        })
    };
    let object = marketplace.as_object_mut().expect("JSON root validated");
    let name = object
        .get("name")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|name| !name.is_empty())
        .ok_or_else(|| anyhow!("现有 marketplace 缺少 name：{}", path.display()))?
        .to_owned();
    match object.get_mut("interface") {
        Some(Value::Object(interface)) => {
            interface
                .entry("displayName")
                .or_insert_with(|| Value::String("Personal".into()));
        }
        None => {
            object.insert("interface".into(), json!({"displayName": "Personal"}));
        }
        _ => bail!("marketplace.interface 必须是对象"),
    }
    let plugins = object
        .entry("plugins")
        .or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut()
        .ok_or_else(|| anyhow!("marketplace.plugins 必须是数组"))?;
    let expected_source = json!({"source": "local", "path": "./plugins/codexbot"});
    if let Some(existing) = plugins
        .iter_mut()
        .find(|entry| entry.get("name").and_then(Value::as_str) == Some(PLUGIN_NAME))
    {
        if existing.get("source") != Some(&expected_source) {
            bail!("现有 codexbot marketplace 条目指向其他来源，已停止以免覆盖");
        }
        let existing = existing.as_object_mut().expect("plugin entry is object");
        existing.insert(
            "policy".into(),
            json!({"installation": "AVAILABLE", "authentication": "ON_INSTALL"}),
        );
        existing.insert("category".into(), Value::String("Productivity".into()));
    } else {
        plugins.push(json!({
            "name": PLUGIN_NAME,
            "source": expected_source,
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"
        }));
    }
    write_json(path, &marketplace, true)?;
    Ok(name)
}

fn path_candidates(name: &str) -> impl Iterator<Item = PathBuf> {
    let path = env::var_os("PATH").unwrap_or_default();
    let extensions: Vec<OsString> = if cfg!(windows) {
        env::var_os("PATHEXT")
            .unwrap_or_else(|| ".COM;.EXE;.BAT;.CMD".into())
            .to_string_lossy()
            .split(';')
            .map(OsString::from)
            .collect()
    } else {
        vec![OsString::new()]
    };
    let mut values = Vec::new();
    for directory in env::split_paths(&path) {
        let direct = directory.join(name);
        values.push(direct.clone());
        if direct.extension().is_none() {
            for extension in &extensions {
                if !extension.is_empty() {
                    values.push(directory.join(format!(
                        "{}{}",
                        name,
                        extension.to_string_lossy().to_ascii_lowercase()
                    )));
                }
            }
        }
    }
    values.into_iter()
}

pub fn find_codex_command() -> Option<PathBuf> {
    ["codex", "codex.exe", "codex.cmd"]
        .into_iter()
        .flat_map(path_candidates)
        .find(|candidate| candidate.is_file())
}

fn run_codex(command: &Path, arguments: &[&str]) -> Result<Output> {
    let mut process = Command::new(command);
    process.args(arguments);
    hide_console_window(&mut process);
    process
        .output()
        .with_context(|| format!("无法启动 {}", command.display()))
}

fn record_marketplace(record: &Map<String, Value>) -> Option<&str> {
    for key in ["marketplace", "marketplaceName", "marketplace_name"] {
        if let Some(value) = record.get(key).and_then(Value::as_str) {
            return Some(value);
        }
    }
    None
}

fn record_matches(value: &Value, plugin_ref: &str, marketplace: &str) -> bool {
    let Some(record) = value.as_object() else {
        return value.as_str().is_some_and(|value| value == plugin_ref);
    };
    let qualified_marketplace = record_marketplace(record);
    for key in [
        "id",
        "ref",
        "identifier",
        "pluginId",
        "plugin_id",
        "fullName",
        "full_name",
        "name",
        "plugin",
        "slug",
    ] {
        if let Some(value) = record.get(key).and_then(Value::as_str) {
            if value == plugin_ref
                || (value == PLUGIN_NAME && qualified_marketplace == Some(marketplace))
            {
                return true;
            }
        }
    }
    ["plugin", "entry"]
        .into_iter()
        .filter_map(|key| record.get(key))
        .any(|nested| record_matches(nested, plugin_ref, marketplace))
}

fn plugin_entries(value: &Value) -> Option<Vec<&Value>> {
    if let Some(array) = value.as_array() {
        return Some(array.iter().collect());
    }
    let object = value.as_object()?;
    for key in [
        "plugins",
        "installed",
        "installedPlugins",
        "installed_plugins",
        "items",
        "results",
        "data",
    ] {
        if let Some(value) = object.get(key) {
            if let Some(entries) = plugin_entries(value) {
                return Some(entries);
            }
        }
    }
    if object.keys().any(|key| {
        [
            "id",
            "name",
            "plugin",
            "ref",
            "identifier",
            "pluginId",
            "plugin_id",
        ]
        .contains(&key.as_str())
    }) {
        return Some(vec![value]);
    }
    None
}

fn query_installed(command: &Path, plugin_ref: &str, marketplace: &str) -> Option<bool> {
    let output = run_codex(command, &["plugin", "list", "--json"]).ok()?;
    if !output.status.success() {
        return None;
    }
    let payload: Value = serde_json::from_slice(&output.stdout).ok()?;
    let entries = plugin_entries(&payload)?;
    Some(
        entries
            .into_iter()
            .any(|entry| record_matches(entry, plugin_ref, marketplace)),
    )
}

fn restore_snapshot(path: &Path, snapshot: &Option<Vec<u8>>) -> Result<()> {
    match snapshot {
        Some(bytes) => write_atomic(path, bytes),
        None => {
            if path.exists() {
                fs::remove_file(path)?;
            }
            Ok(())
        }
    }
}

pub fn install_personal_plugin(
    repo_root: &Path,
    home: Option<&Path>,
    run_codex_registration: bool,
    permission_notifications: bool,
) -> Result<InstallResult> {
    let source = repo_root.join("plugin").join(PLUGIN_NAME);
    validate_plugin_tree(&source)?;
    let home = home.map(Path::to_path_buf).unwrap_or_else(|| {
        env::var_os("USERPROFILE")
            .or_else(|| env::var_os("HOME"))
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."))
    });
    let plugin_parent = home.join("plugins");
    let plugin_path = plugin_parent.join(PLUGIN_NAME);
    let marketplace_path = home
        .join(".agents")
        .join("plugins")
        .join("marketplace.json");
    let marketplace_bak = marketplace_backup(&marketplace_path);

    if plugin_path.exists() {
        let manifest = plugin_path.join(".codex-plugin").join("plugin.json");
        if !manifest.is_file()
            || read_json(&manifest)?.get("name").and_then(Value::as_str) != Some(PLUGIN_NAME)
        {
            bail!(
                "目标插件目录不是 CodexBot，拒绝覆盖：{}",
                plugin_path.display()
            );
        }
    }

    fs::create_dir_all(&plugin_parent)?;
    let transaction = plugin_parent.join(format!(".codexbot-install-{}", Uuid::new_v4()));
    let staged = transaction.join(PLUGIN_NAME);
    let previous = transaction.join("previous");
    fs::create_dir_all(&transaction)?;
    copy_tree(&source, &staged)?;
    materialize_hooks(&staged, &plugin_path, permission_notifications)?;
    cachebust_manifest(&staged)?;

    let marketplace_snapshot = fs::read(&marketplace_path).ok();
    let backup_snapshot = fs::read(&marketplace_bak).ok();
    let had_previous = plugin_path.exists();
    let operation = (|| -> Result<InstallResult> {
        if had_previous {
            fs::rename(&plugin_path, &previous)?;
        }
        fs::rename(&staged, &plugin_path)?;
        let marketplace_name = merge_marketplace(&marketplace_path)?;
        let mut codex_output = String::new();
        if run_codex_registration {
            let command = find_codex_command()
                .ok_or_else(|| anyhow!("找不到 codex/codex.cmd；安装已回滚，未注册到 Codex"))?;
            let plugin_ref = format!("{PLUGIN_NAME}@{marketplace_name}");
            let was_installed = query_installed(&command, &plugin_ref, &marketplace_name)
                .ok_or_else(|| anyhow!("codex plugin list --json 无法安全判断现有安装状态"))?;
            let output = run_codex(&command, &["plugin", "add", &plugin_ref, "--json"])?;
            codex_output = String::from_utf8_lossy(if output.stdout.is_empty() {
                &output.stderr
            } else {
                &output.stdout
            })
            .trim()
            .chars()
            .take(2_000)
            .collect();
            if !output.status.success() {
                if !was_installed
                    && query_installed(&command, &plugin_ref, &marketplace_name) == Some(true)
                {
                    let _ = run_codex(&command, &["plugin", "remove", &plugin_ref, "--json"]);
                }
                bail!("codex plugin add 失败：{codex_output}");
            }
        }
        Ok(InstallResult {
            plugin_path: plugin_path.clone(),
            marketplace_path: marketplace_path.clone(),
            marketplace_name,
            codex_output,
        })
    })();

    match operation {
        Ok(result) => {
            let _ = fs::remove_dir_all(&transaction);
            Ok(result)
        }
        Err(error) => {
            if plugin_path.exists() {
                let _ = fs::remove_dir_all(&plugin_path);
            }
            if previous.exists() {
                let _ = fs::rename(&previous, &plugin_path);
            }
            let restore_result = restore_snapshot(&marketplace_path, &marketplace_snapshot)
                .and_then(|_| restore_snapshot(&marketplace_bak, &backup_snapshot));
            let _ = fs::remove_dir_all(&transaction);
            if let Err(rollback_error) = restore_result {
                bail!("{error}；安装回滚失败：{rollback_error}");
            }
            Err(error)
        }
    }
}

pub fn marketplace_contains_plugin(path: &Path) -> bool {
    let Ok(value) = read_json(path) else {
        return false;
    };
    value
        .get("plugins")
        .and_then(Value::as_array)
        .is_some_and(|plugins| {
            plugins.iter().any(|entry| {
                entry.get("name").and_then(Value::as_str) == Some(PLUGIN_NAME)
                    && entry.get("source")
                        == Some(&json!({"source": "local", "path": "./plugins/codexbot"}))
            })
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
    }

    #[test]
    fn source_plugin_is_valid() {
        validate_plugin_tree(&source_root().join("plugin").join("codexbot")).unwrap();
    }

    #[test]
    fn install_merges_marketplace_and_is_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let home = directory.path().join("home with 空格");
        let marketplace = home.join(".agents/plugins/marketplace.json");
        fs::create_dir_all(marketplace.parent().unwrap()).unwrap();
        fs::write(
            &marketplace,
            br#"{"name":"mine","plugins":[{"name":"existing","source":{"source":"local","path":"./plugins/existing"}}]}"#,
        )
        .unwrap();
        let first = install_personal_plugin(&source_root(), Some(&home), false, false).unwrap();
        let second = install_personal_plugin(&source_root(), Some(&home), false, false).unwrap();
        assert_eq!(first.plugin_path, second.plugin_path);
        let value = read_json(&marketplace).unwrap();
        assert_eq!(
            value["plugins"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|entry| entry["name"] == PLUGIN_NAME)
                .count(),
            1
        );
        let hooks = read_json(&first.plugin_path.join("hooks/hooks.json")).unwrap();
        assert_eq!(hook_names(&hooks).unwrap().len(), CORE_HOOK_EVENTS.len());
    }
}
