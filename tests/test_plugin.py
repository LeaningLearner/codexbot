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
        assert hook["commandWindows"].startswith("cmd /d /s /c")
        assert "${PLUGIN_ROOT}\\hooks\\entry.cmd" in hook["commandWindows"]
        assert "python " not in hook["commandWindows"]
        assert hook["timeout"] <= 2
        assert set(hook) == {"type", "command", "commandWindows", "timeout"}

    assert (PLUGIN / "hooks" / "entry.cmd").is_file()


def test_install_path_locks_runtime_and_pep517_build_dependencies() -> None:
    install_script = (ROOT / "install.cmd").read_text(encoding="utf-8")
    assert "--only-binary=:all:" in install_script
    assert "--require-hashes" in install_script
    assert "--no-build-isolation" in install_script

    lock_lines = [
        line.strip()
        for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert lock_lines
    assert all(" --hash=sha256:" in line for line in lock_lines)
    assert any(line.startswith("setuptools==75.8.0 ") for line in lock_lines)
    assert any(line.startswith("wheel==0.45.1 ") for line in lock_lines)


def test_manifest_uses_discovery_instead_of_unsupported_hook_field() -> None:
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["name"] == PLUGIN.name
    assert "hooks" not in manifest
    assert "mcpServers" not in manifest
    assert "apps" not in manifest
