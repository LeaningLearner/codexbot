from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin" / "codexbot"


def test_hook_registration_is_complete_and_neutral() -> None:
    payload = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    expected = {
        "SessionStart",
        "UserPromptSubmit",
        "PermissionRequest",
        "PostToolUse",
        "Stop",
        "SessionEnd",
    }

    assert set(payload["hooks"]) == expected
    for event in expected:
        registrations = payload["hooks"][event]
        assert len(registrations) == 1
        hook = registrations[0]["hooks"][0]
        assert hook["type"] == "command"
        assert "${PLUGIN_ROOT}" in hook["commandWindows"]
        assert hook["timeout"] <= 2
        assert set(hook) == {"type", "command", "commandWindows", "timeout"}


def test_manifest_uses_discovery_instead_of_unsupported_hook_field() -> None:
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["name"] == PLUGIN.name
    assert "hooks" not in manifest
    assert "mcpServers" not in manifest
    assert "apps" not in manifest
