from __future__ import annotations

from pathlib import Path
import subprocess

import codexbot.installer as installer
import codexbot.subprocess_utils as subprocess_utils


def test_hidden_console_kwargs_are_omitted_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.os, "name", "posix")

    assert subprocess_utils.hidden_console_subprocess_kwargs() == {}


def test_hidden_console_kwargs_include_no_window_and_process_group(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.os, "name", "nt")
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(
        subprocess_utils.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x00000200,
        raising=False,
    )

    kwargs = subprocess_utils.hidden_console_subprocess_kwargs(new_process_group=True)

    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["creationflags"] & 0x00000200
    if hasattr(subprocess, "STARTUPINFO"):
        assert kwargs["startupinfo"].wShowWindow == getattr(subprocess, "SW_HIDE", 0)


def test_npm_native_codex_binary_is_resolved_from_the_shim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.os, "name", "nt")
    monkeypatch.setattr(subprocess_utils.platform, "machine", lambda: "AMD64")
    shim = tmp_path / "bin" / "codex.cmd"
    shim.parent.mkdir()
    shim.write_text("@echo off", encoding="utf-8")
    native = (
        shim.parent
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")

    assert subprocess_utils.npm_codex_native_executable(shim) == str(native)


def test_find_codex_command_prefers_npm_native_and_skips_windowsapps(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(installer.os, "name", "nt")
    monkeypatch.setattr(subprocess_utils.os, "name", "nt")
    monkeypatch.setattr(subprocess_utils.platform, "machine", lambda: "AMD64")
    shim = tmp_path / "codex.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"MZ")

    def which(name: str) -> str | None:
        if name == "codex.cmd":
            return str(shim)
        if name == "codex.exe":
            return r"C:\Program Files\WindowsApps\OpenAI.Codex_x64\app\resources\codex.exe"
        return None

    monkeypatch.setattr(installer.shutil, "which", which)
    assert installer.find_codex_command() == str(native)

    native.unlink()
    assert installer.find_codex_command() == str(shim)


def test_find_codex_command_does_not_fall_back_to_windowsapps_resource(
    monkeypatch,
) -> None:
    monkeypatch.setattr(installer.os, "name", "nt")
    windowsapps = (
        r"C:\Program Files\WindowsApps\OpenAI.Codex_x64"
        r"__2p2nqsd0c76g0\app\resources\codex"
    )

    def which(name: str) -> str | None:
        if name == "codex.cmd":
            return None
        if name == "codex":
            return windowsapps
        if name == "codex.exe":
            return windowsapps + ".exe"
        return None

    monkeypatch.setattr(installer.shutil, "which", which)

    assert installer.find_codex_command() is None


def test_installer_codex_run_receives_hidden_window_options(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.os, "name", "nt")
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    installer._run_codex_json("codex.cmd", ["plugin", "list", "--json"], timeout=5)

    assert int(captured["creationflags"]) & 0x08000000
