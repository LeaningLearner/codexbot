from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from codexbot.codex_hooks import (
    format_hook_trust,
    hook_state_key,
    load_hook_declarations,
    read_hook_trust,
)


EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)


def _write_hooks_json(path: Path, events: Iterable[str] = EVENTS) -> None:
    hooks = {event: [{"hooks": [{"type": "command", "command": "true"}]}] for event in events}
    path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def _write_config(codex_home: Path, records: dict[str, dict[str, Any]]) -> None:
    lines = ["[hooks.state]"]
    for key, fields in records.items():
        lines.append(f'[hooks.state."{key}"]')
        for name, value in fields.items():
            if isinstance(value, bool):
                lines.append(f"{name} = {'true' if value else 'false'}")
            else:
                lines.append(f'{name} = "{value}"')
    lines.append("")
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _trusted_records() -> dict[str, dict[str, Any]]:
    keys = [hook_state_key("codexbot@personal:hooks/hooks.json", event, 0, 0) for event in EVENTS]
    return {key: {"trusted_hash": f"sha256:{event}"} for key, event in zip(keys, EVENTS)}


def test_all_trusted_hooks_reported_ok(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    _write_hooks_json(hooks_file)
    codex_home = tmp_path / "codex"
    records = _trusted_records()
    records[hook_state_key("codexbot@personal:hooks/hooks.json", "SessionStart", 0, 0)]["enabled"] = True
    _write_config(codex_home, records)

    entries = read_hook_trust(codex_home, hooks_file, plugin_name="codexbot")

    assert len(entries) == len(EVENTS)
    assert all(entry.trusted and entry.enabled for entry in entries)
    assert format_hook_trust(entries) == (True, "6/6 个 hook 已信任并启用")


def test_missing_trust_record_reported_untrusted(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    _write_hooks_json(hooks_file, events=("SessionStart", "Stop"))
    codex_home = tmp_path / "codex"
    _write_config(
        codex_home,
        {hook_state_key("codexbot@personal:hooks/hooks.json", "SessionStart", 0, 0): {"trusted_hash": "sha256:x"}},
    )

    entries = read_hook_trust(codex_home, hooks_file, plugin_name="codexbot")

    by_event = {entry.event: entry for entry in entries}
    assert by_event["SessionStart"].trusted
    assert not by_event["Stop"].trusted
    ok, detail = format_hook_trust(entries)
    assert not ok
    assert "未信任：Stop" in detail


def test_disabled_hook_reported_disabled(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    _write_hooks_json(hooks_file, events=("Stop",))
    codex_home = tmp_path / "codex"
    key = hook_state_key("codexbot@personal:hooks/hooks.json", "Stop", 0, 0)
    _write_config(codex_home, {key: {"trusted_hash": "sha256:x", "enabled": False}})

    entries = read_hook_trust(codex_home, hooks_file, plugin_name="codexbot")

    assert entries[0].trusted
    assert not entries[0].enabled
    ok, detail = format_hook_trust(entries)
    assert not ok
    assert "未启用：Stop" in detail


def test_legacy_entry_without_enabled_defaults_to_enabled(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    _write_hooks_json(hooks_file, events=("Stop",))
    codex_home = tmp_path / "codex"
    key = hook_state_key("codexbot@personal:hooks/hooks.json", "Stop", 0, 0)
    _write_config(codex_home, {key: {"trusted_hash": "sha256:x"}})

    entries = read_hook_trust(codex_home, hooks_file, plugin_name="codexbot")

    assert entries[0].trusted
    assert entries[0].enabled


def test_missing_config_toml_reports_untrusted(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    _write_hooks_json(hooks_file, events=("Stop",))

    entries = read_hook_trust(tmp_path / "missing-codex", hooks_file, plugin_name="codexbot")

    assert entries and not entries[0].trusted


def test_missing_hooks_file_returns_empty(tmp_path: Path) -> None:
    entries = read_hook_trust(tmp_path / "codex", tmp_path / "missing.json", plugin_name="codexbot")

    assert entries == []
    assert format_hook_trust(entries) == (False, "未找到插件 hooks.json 声明")


def test_hook_state_key_maps_event_to_snake_case() -> None:
    key = hook_state_key("codexbot@personal:hooks/hooks.json", "UserPromptSubmit", 0, 0)

    assert key == "codexbot@personal:hooks/hooks.json:user_prompt_submit:0:0"


def test_group_and_hook_indices_are_preserved(tmp_path: Path) -> None:
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "a"}]},
                        {"hooks": [{"type": "command", "command": "b"}, {"type": "command", "command": "c"}]},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    declarations = load_hook_declarations(hooks_file)

    assert declarations == [("SessionStart", 0, 0), ("SessionStart", 1, 0), ("SessionStart", 1, 1)]
