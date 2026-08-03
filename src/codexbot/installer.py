from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


PLUGIN_NAME = "codexbot"
REQUIRED_HOOK_EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}


@dataclass(frozen=True)
class InstallResult:
    plugin_path: Path
    marketplace_path: Path
    marketplace_name: str
    codex_output: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"无法解析 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} 的根节点必须是 JSON 对象")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any], *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".codexbot.bak"))
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def validate_plugin_tree(plugin_path: Path) -> None:
    manifest_path = plugin_path / ".codex-plugin" / "plugin.json"
    hooks_path = plugin_path / "hooks" / "hooks.json"
    if not manifest_path.is_file() or not hooks_path.is_file():
        raise RuntimeError(f"插件结构不完整：{plugin_path}")

    manifest = _load_json(manifest_path)
    if manifest.get("name") != PLUGIN_NAME or plugin_path.name != PLUGIN_NAME:
        raise RuntimeError("插件目录名与 manifest name 必须都是 codexbot")
    for field in ("version", "description"):
        if not str(manifest.get(field) or "").strip():
            raise RuntimeError(f"plugin.json 缺少必填字段：{field}")
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        raise RuntimeError("plugin.json.interface 必须是对象")
    for field in ("displayName", "shortDescription", "defaultPrompt"):
        if not str(interface.get(field) or "").strip():
            raise RuntimeError(f"plugin.json.interface 缺少必填字段：{field}")
    if "hooks" in manifest:
        raise RuntimeError("plugin.json 不支持 hooks 字段；应使用默认 hooks/hooks.json 发现")

    hooks_document = _load_json(hooks_path)
    hooks = hooks_document.get("hooks")
    if not isinstance(hooks, dict) or set(hooks) != REQUIRED_HOOK_EVENTS:
        raise RuntimeError("hooks.json 必须且只能注册六个 CodexBot 生命周期事件")
    for event_name in REQUIRED_HOOK_EVENTS:
        groups = hooks.get(event_name)
        if not isinstance(groups, list) or not groups:
            raise RuntimeError(f"Hook 未配置：{event_name}")
        handlers = groups[0].get("hooks") if isinstance(groups[0], dict) else None
        if not isinstance(handlers, list) or not handlers:
            raise RuntimeError(f"Hook 没有命令处理器：{event_name}")
        handler = handlers[0]
        if not isinstance(handler, dict) or handler.get("type") != "command":
            raise RuntimeError(f"Hook 处理器类型无效：{event_name}")
        windows_command = str(handler.get("commandWindows") or "")
        if "${PLUGIN_ROOT}" not in windows_command:
            raise RuntimeError(f"Windows Hook 必须通过 PLUGIN_ROOT 启动：{event_name}")
        try:
            timeout = float(handler.get("timeout"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Hook timeout 无效：{event_name}") from exc
        if timeout <= 0 or timeout > 2:
            raise RuntimeError(f"Hook timeout 必须在 0-2 秒内：{event_name}")


def _cachebust_manifest(plugin_path: Path) -> str:
    manifest_path = plugin_path / ".codex-plugin" / "plugin.json"
    manifest = _load_json(manifest_path)
    if manifest.get("name") != PLUGIN_NAME:
        raise RuntimeError(f"插件名称不匹配：{manifest.get('name')!r}")
    base_version = str(manifest.get("version") or "0.1.0").split("+", 1)[0]
    stamp = datetime.now(timezone.utc).strftime("local-%Y%m%d-%H%M%S-%f")
    version = f"{base_version}+codex.{stamp}"
    manifest["version"] = version
    _write_json_atomic(manifest_path, manifest, backup=False)
    return version


def _merge_personal_marketplace(path: Path) -> str:
    changed = False
    if path.exists():
        marketplace = _load_json(path)
        name = str(marketplace.get("name") or "").strip()
        if not name:
            raise RuntimeError(f"现有 marketplace 缺少 name：{path}")
        interface = marketplace.get("interface")
        if interface is None:
            interface = {}
            marketplace["interface"] = interface
            changed = True
        if not isinstance(interface, dict):
            raise RuntimeError("marketplace.interface 必须是对象")
        if "displayName" not in interface:
            interface["displayName"] = "Personal"
            changed = True
        plugins = marketplace.get("plugins")
        if plugins is None:
            plugins = []
            marketplace["plugins"] = plugins
            changed = True
        if not isinstance(plugins, list):
            raise RuntimeError("marketplace.plugins 必须是数组")
    else:
        name = "personal"
        marketplace = {"name": name, "interface": {"displayName": "Personal"}, "plugins": []}
        plugins = marketplace["plugins"]

    expected_source = {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"}
    existing = next(
        (entry for entry in plugins if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME),
        None,
    )
    if existing is not None:
        if existing.get("source") != expected_source:
            raise RuntimeError("现有 codexbot marketplace 条目指向其他来源，已停止以免覆盖")
        expected_policy = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
        if existing.get("policy") != expected_policy:
            existing["policy"] = expected_policy
            changed = True
        if existing.get("category") != "Productivity":
            existing["category"] = "Productivity"
            changed = True
    else:
        plugins.append(
            {
                "name": PLUGIN_NAME,
                "source": expected_source,
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Productivity",
            }
        )
        changed = True

    if changed:
        _write_json_atomic(path, marketplace)
    return name


def find_codex_command() -> str | None:
    return shutil.which("codex.cmd") or shutil.which("codex")


def install_personal_plugin(
    repo_root: Path,
    *,
    home: Path | None = None,
    run_codex: bool = True,
) -> InstallResult:
    repo_root = Path(repo_root).resolve()
    source = repo_root / "plugin" / PLUGIN_NAME
    validate_plugin_tree(source)

    home = Path.home() if home is None else Path(home).resolve()
    plugin_path = home / "plugins" / PLUGIN_NAME
    if plugin_path.exists():
        existing_manifest = plugin_path / ".codex-plugin" / "plugin.json"
        if not existing_manifest.is_file() or _load_json(existing_manifest).get("name") != PLUGIN_NAME:
            raise RuntimeError(f"目标目录不是可识别的 codexbot 插件，拒绝覆盖：{plugin_path}")
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        plugin_path,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for cache in plugin_path.rglob("__pycache__"):
        shutil.rmtree(cache)
    for compiled in (*plugin_path.rglob("*.pyc"), *plugin_path.rglob("*.pyo")):
        compiled.unlink()
    _cachebust_manifest(plugin_path)
    validate_plugin_tree(plugin_path)

    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_name = _merge_personal_marketplace(marketplace_path)

    output = ""
    if run_codex:
        command = find_codex_command()
        if not command:
            raise RuntimeError("找不到 codex/codex.cmd，插件文件已复制但尚未安装到 Codex")
        completed = subprocess.run(
            [command, "plugin", "add", f"{PLUGIN_NAME}@{marketplace_name}", "--json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=60,
            check=False,
        )
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode:
            raise RuntimeError(f"codex plugin add 失败：{output}")

    return InstallResult(
        plugin_path=plugin_path,
        marketplace_path=marketplace_path,
        marketplace_name=marketplace_name,
        codex_output=output,
    )


def marketplace_contains_plugin(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        marketplace = _load_json(path)
    except RuntimeError:
        return False
    plugins = marketplace.get("plugins")
    return isinstance(plugins, list) and any(
        isinstance(entry, dict)
        and entry.get("name") == PLUGIN_NAME
        and entry.get("source") == {"source": "local", "path": "./plugins/codexbot"}
        for entry in plugins
    )
