from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


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
        assert hook["commandWindows"].startswith("powershell.exe")
        assert "-WindowStyle Hidden" in hook["commandWindows"]
        assert "${PLUGIN_ROOT}\\hooks\\entry.ps1" in hook["commandWindows"]
        assert "python " not in hook["commandWindows"]
        assert hook["timeout"] <= 2
        assert set(hook) == {"type", "command", "commandWindows", "timeout"}

    assert (PLUGIN / "hooks" / "entry.cmd").is_file()
    assert (PLUGIN / "hooks" / "entry.ps1").is_file()
    entry_cmd = (PLUGIN / "hooks" / "entry.cmd").read_text(encoding="utf-8")
    assert "pythonw.exe" in entry_cmd


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


def test_install_cmd_uses_consistent_crlf_line_endings() -> None:
    content = (ROOT / "install.cmd").read_bytes()

    assert b"\r\n" in content
    assert b"\n" not in content.replace(b"\r\n", b"")


@pytest.mark.skipif(os.name != "nt", reason="Windows batch parser regression")
def test_install_cmd_parse_only_falls_back_when_py_launcher_is_broken(tmp_path: Path) -> None:
    shim_dir = tmp_path / "python-shims"
    shim_dir.mkdir()
    shutil.copyfile(
        Path(os.environ["SystemRoot"]) / "System32" / "where.exe",
        shim_dir / "py.exe",
    )
    environment = os.environ.copy()
    environment["PATH"] = str(shim_dir) + os.pathsep + environment.get("PATH", "")
    environment["CODEXBOT_PARSE_ONLY"] = "1"
    environment["CODEXBOT_NO_PAUSE"] = "1"

    completed = subprocess.run(
        f'cmd.exe /d /c call "{ROOT / "install.cmd"}"',
        cwd=ROOT,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "[OK] install.cmd" in output
    assert "unexpected at this time" not in output.casefold()


def test_manifest_uses_discovery_instead_of_unsupported_hook_field() -> None:
    manifest = json.loads(
        (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["name"] == PLUGIN.name
    assert "hooks" not in manifest
    assert "mcpServers" not in manifest
    assert "apps" not in manifest
