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
CODEX_PLUGIN_LIST_TIMEOUT_SECONDS = 15
CODEX_PLUGIN_ADD_TIMEOUT_SECONDS = 60
CODEX_PLUGIN_REMOVE_TIMEOUT_SECONDS = 15
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


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    content: bytes | None


def _snapshot_file(path: Path) -> _FileSnapshot:
    if not path.is_file():
        return _FileSnapshot(False, None)
    return _FileSnapshot(True, path.read_bytes())


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    if not snapshot.exists:
        if path.is_file() or path.is_symlink():
            path.unlink()
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=path.name + ".restore-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(snapshot.content or b"")
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


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
    temporary: Path | None = None
    try:
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
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


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


def _remove_plugin_artifacts(plugin_path: Path) -> None:
    for cache in tuple(plugin_path.rglob("__pycache__")):
        if cache.is_dir() and not cache.is_symlink():
            shutil.rmtree(cache)
    for compiled in (*plugin_path.rglob("*.pyc"), *plugin_path.rglob("*.pyo")):
        if compiled.is_file() or compiled.is_symlink():
            compiled.unlink()


def _copy_plugin_tree(source: Path, destination: Path) -> None:
    """Build a clean plugin tree instead of merging into an existing one."""

    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    _remove_plugin_artifacts(destination)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _marketplace_backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".codexbot.bak")


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


def _run_codex_json(
    command: str,
    arguments: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [command, *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _plugin_list_entries(payload: object, plugin_ref: str) -> tuple[list[object], bool]:
    """Return list entries and whether the CLI shape was recognized.

    Codex CLI versions have used both a top-level array and an object containing
    ``plugins``/``data``. Unknown output is deliberately reported as unknown
    instead of an empty list so a failed snapshot can never authorize removal.
    """

    if isinstance(payload, list):
        return payload, True
    if not isinstance(payload, dict):
        return [], False
    if plugin_ref in payload:
        return [plugin_ref], True

    for key in (
        "plugins",
        "installed",
        "installedPlugins",
        "installed_plugins",
        "items",
        "results",
        "data",
    ):
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, list):
            return value, True
        entries, recognized = _plugin_list_entries(value, plugin_ref)
        if recognized:
            return entries, True

    record_fields = {
        "id",
        "name",
        "plugin",
        "ref",
        "identifier",
        "pluginId",
        "plugin_id",
        "fullName",
        "full_name",
    }
    if record_fields.intersection(payload):
        return [payload], True
    return [], False


def _marketplace_value(record: dict[str, object]) -> str | None:
    for key in ("marketplace", "marketplaceName", "marketplace_name"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("name", "id", "slug"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value.strip()
    source = record.get("source")
    if isinstance(source, dict):
        for nested_key in ("marketplace", "name", "id", "slug"):
            nested_value = source.get(nested_key)
            if isinstance(nested_value, str) and nested_value.strip():
                return nested_value.strip()
    return None


def _plugin_record_matches(
    record: object,
    plugin_ref: str,
    marketplace_name: str,
    inherited_marketplace: str | None = None,
) -> bool:
    if isinstance(record, str):
        return record.strip() == plugin_ref
    if not isinstance(record, dict):
        return False

    current_marketplace = _marketplace_value(record) or inherited_marketplace
    for key in (
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
    ):
        value = record.get(key)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized == plugin_ref:
                return True
            if normalized == PLUGIN_NAME and current_marketplace == marketplace_name:
                return True

    for key in ("plugin", "entry"):
        nested = record.get(key)
        if _plugin_record_matches(
            nested,
            plugin_ref,
            marketplace_name,
            current_marketplace,
        ):
            return True
    return False


def _plugin_record_has_unqualified_target(
    record: object,
    marketplace_name: str,
    inherited_marketplace: str | None = None,
) -> bool:
    """Detect a target-looking record that lacks enough data to be safe."""

    if isinstance(record, str):
        return record.strip() == PLUGIN_NAME
    if not isinstance(record, dict):
        return False

    current_marketplace = _marketplace_value(record) or inherited_marketplace
    for key in (
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
    ):
        value = record.get(key)
        if isinstance(value, str) and value.strip() == PLUGIN_NAME:
            if current_marketplace is None:
                return True
            if current_marketplace == marketplace_name:
                return False

    for key in ("plugin", "entry"):
        if _plugin_record_has_unqualified_target(
            record.get(key),
            marketplace_name,
            current_marketplace,
        ):
            return True
    return False


def _query_codex_plugin_installed(
    command: str,
    plugin_ref: str,
    marketplace_name: str,
) -> bool | None:
    """Return installed state, or ``None`` when a safe answer is unavailable."""

    try:
        completed = _run_codex_json(
            command,
            ["plugin", "list", "--json"],
            timeout=CODEX_PLUGIN_LIST_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode:
        return None
    try:
        payload = json.loads(completed.stdout or "")
    except (TypeError, json.JSONDecodeError):
        return None
    entries, recognized = _plugin_list_entries(payload, plugin_ref)
    if not recognized:
        return None
    if any(_plugin_record_matches(entry, plugin_ref, marketplace_name) for entry in entries):
        return True
    if any(
        _plugin_record_has_unqualified_target(entry, marketplace_name) for entry in entries
    ):
        return None
    return False


def _codex_output(completed: subprocess.CompletedProcess[str]) -> str:
    output = completed.stdout or completed.stderr or ""
    return str(output).strip()[:2000]


def _remove_new_codex_plugin(command: str, plugin_ref: str) -> str | None:
    try:
        completed = _run_codex_json(
            command,
            ["plugin", "remove", plugin_ref, "--json"],
            timeout=CODEX_PLUGIN_REMOVE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "codex plugin remove timed out"
    except OSError as exc:
        return f"codex plugin remove could not start: {type(exc).__name__}"
    if completed.returncode:
        output = _codex_output(completed)
        return f"codex plugin remove failed{': ' + output if output else ''}"
    return None


def _recover_failed_codex_add(
    command: str,
    plugin_ref: str,
    marketplace_name: str,
    was_installed: bool,
    failure_message: str,
) -> str:
    """Remove only a registration proven to have appeared during this add."""

    installed_after = _query_codex_plugin_installed(command, plugin_ref, marketplace_name)
    if was_installed is False and installed_after is True:
        removal_error = _remove_new_codex_plugin(command, plugin_ref)
        if removal_error:
            return f"{failure_message}; {removal_error}"
    return failure_message


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
            raise RuntimeError(
                f"目标目录不是可识别的 codexbot 插件，拒绝覆盖：{plugin_path}"
            )
    plugin_path.parent.mkdir(parents=True, exist_ok=True)

    marketplace_path = home / ".agents" / "plugins" / "marketplace.json"
    marketplace_backup_path = _marketplace_backup_path(marketplace_path)
    marketplace_snapshot = _snapshot_file(marketplace_path)
    marketplace_backup_snapshot = _snapshot_file(marketplace_backup_path)

    transaction_root = Path(
        tempfile.mkdtemp(prefix=f".{PLUGIN_NAME}-install-", dir=plugin_path.parent)
    )
    # The validator intentionally checks the directory name as well as the
    # manifest name, so keep the staging directory named like the final plugin.
    staged_plugin = transaction_root / PLUGIN_NAME
    previous_plugin = transaction_root / "previous-plugin"
    had_previous_plugin = plugin_path.exists()
    previous_plugin_moved = False
    plugin_swapped = False
    rollback_cleanup = True

    try:
        # Build from an empty directory so source deletions remove target stale files.
        _copy_plugin_tree(source, staged_plugin)
        _cachebust_manifest(staged_plugin)
        validate_plugin_tree(staged_plugin)

        if had_previous_plugin:
            plugin_path.replace(previous_plugin)
            previous_plugin_moved = True
        staged_plugin.replace(plugin_path)
        plugin_swapped = True

        marketplace_name = _merge_personal_marketplace(marketplace_path)
        output = ""
        if run_codex:
            command = find_codex_command()
            if not command:
                raise RuntimeError(
                    "找不到 codex/codex.cmd；安装已回滚，未安装到 Codex"
                )
            plugin_ref = f"{PLUGIN_NAME}@{marketplace_name}"
            was_installed = _query_codex_plugin_installed(
                command,
                plugin_ref,
                marketplace_name,
            )
            if was_installed is None:
                raise RuntimeError(
                    "codex plugin list --json cannot safely determine the existing state; "
                    "installation stopped to avoid removing an existing plugin"
                )
            try:
                completed = _run_codex_json(
                    command,
                    ["plugin", "add", plugin_ref, "--json"],
                    timeout=CODEX_PLUGIN_ADD_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                failure = _recover_failed_codex_add(
                    command,
                    plugin_ref,
                    marketplace_name,
                    was_installed,
                    "codex plugin add timed out",
                )
                raise RuntimeError(failure)
            except OSError as exc:
                failure = _recover_failed_codex_add(
                    command,
                    plugin_ref,
                    marketplace_name,
                    was_installed,
                    f"codex plugin add could not start: {type(exc).__name__}",
                )
                raise RuntimeError(failure) from exc
            output = _codex_output(completed)
            if completed.returncode:
                failure = _recover_failed_codex_add(
                    command,
                    plugin_ref,
                    marketplace_name,
                    was_installed,
                    f"codex plugin add failed{': ' + output if output else ''}",
                )
                raise RuntimeError(failure)

        return InstallResult(
            plugin_path=plugin_path,
            marketplace_path=marketplace_path,
            marketplace_name=marketplace_name,
            codex_output=output,
        )
    except BaseException as exc:
        try:
            if plugin_swapped:
                _remove_path(plugin_path)
            if previous_plugin_moved and previous_plugin.exists():
                previous_plugin.replace(plugin_path)
            _restore_file(marketplace_path, marketplace_snapshot)
            _restore_file(marketplace_backup_path, marketplace_backup_snapshot)
        except Exception as rollback_error:
            # Retain the transaction directory when recovery fails so the old
            # plugin tree remains available for manual recovery.
            rollback_cleanup = False
            raise RuntimeError(f"{exc}；安装回滚失败：{rollback_error}") from exc
        raise
    finally:
        if rollback_cleanup:
            shutil.rmtree(transaction_root, ignore_errors=True)


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
