from __future__ import annotations

import json
import os
import subprocess

import codexbot.cli as cli


def test_doctor_codex_probe_uses_hidden_window_options(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "find_codex_command", lambda: "codex.cmd")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"installed": []}),
            stderr="",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    installed, _detail = cli._codex_plugin_installed()

    assert installed is False
    if os.name == "nt":
        assert int(captured["creationflags"]) & subprocess.CREATE_NO_WINDOW
        assert "startupinfo" in captured
