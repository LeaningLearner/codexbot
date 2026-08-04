from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import tomllib
from typing import Any


def codex_home_dir() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def hook_state_key(
    source: str,
    event: str,
    group_index: int,
    hook_index: int,
) -> str:
    """Build the Codex ``[hooks.state]`` key for one hook entry."""

    return f"{source}:{_snake_case(event)}:{group_index}:{hook_index}"


@dataclass(frozen=True)
class HookTrustEntry:
    event: str
    trusted: bool
    enabled: bool


def load_hook_declarations(hooks_file: Path) -> list[tuple[str, int, int]]:
    """Return ``(event, group_index, hook_index)`` triples from a hooks.json."""

    try:
        document = json.loads(hooks_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return []

    declarations: list[tuple[str, int, int]] = []
    for event, groups in hooks.items():
        if not isinstance(groups, list) or not groups:
            continue
        for group_index, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list) or not handlers:
                declarations.append((event, group_index, 0))
                continue
            for hook_index in range(len(handlers)):
                declarations.append((event, group_index, hook_index))
    return declarations


def read_hook_trust(
    codex_home: Path,
    hooks_file: Path,
    *,
    plugin_name: str,
) -> list[HookTrustEntry]:
    """Match declared plugin hooks against Codex's recorded trust state.

    Codex stores hook trust in ``config.toml`` under ``[hooks.state]`` keys
    such as ``codexbot@personal:hooks/hooks.json:session_start:0:0``.  A hook
    counts as trusted when its entry has a ``trusted_hash``; the ``enabled``
    flag defaults to on for older entries recorded before the flag existed.
    """

    state: dict[str, Any] = {}
    config_path = codex_home / "config.toml"
    if config_path.is_file():
        try:
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
        except (OSError, ValueError):
            config = {}
        raw_state = config.get("hooks", {}).get("state")
        if isinstance(raw_state, dict):
            state = raw_state

    source = f"{plugin_name}@personal:hooks/hooks.json"
    entries: list[HookTrustEntry] = []
    for event, group_index, hook_index in load_hook_declarations(hooks_file):
        key = hook_state_key(source, event, group_index, hook_index)
        record = state.get(key)
        if not isinstance(record, dict):
            entries.append(HookTrustEntry(event, False, False))
            continue
        trusted = bool(record.get("trusted_hash"))
        enabled = bool(record.get("enabled", True))
        entries.append(HookTrustEntry(event, trusted, enabled))
    return entries


def format_hook_trust(entries: list[HookTrustEntry]) -> tuple[bool, str]:
    """Summarize hook trust for doctor output as ``(ok, detail)``."""

    if not entries:
        return False, "未找到插件 hooks.json 声明"
    trusted = all(entry.trusted for entry in entries)
    enabled = all(entry.enabled for entry in entries)
    if trusted and enabled:
        return True, f"{len(entries)}/{len(entries)} 个 hook 已信任并启用"

    parts: list[str] = []
    untrusted = [entry.event for entry in entries if not entry.trusted]
    disabled = [entry.event for entry in entries if not entry.enabled]
    if untrusted:
        parts.append("未信任：" + "、".join(untrusted))
    if disabled:
        parts.append("未启用：" + "、".join(disabled))
    return False, "；".join(parts)
