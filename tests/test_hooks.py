from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _load_bootstrap():
    path = ROOT / "plugin" / "codexbot" / "hooks" / "entry.py"
    spec = importlib.util.spec_from_file_location("codexbot_hook_bootstrap", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_removes_foreign_python_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PYTHONHOME", r"C:\Program Files\LibreOffice\python-core")
    monkeypatch.setenv("PYTHONPATH", r"C:\Program Files\LibreOffice\program")
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("CODEXBOT_SENTINEL", "preserved")

    environment = _load_bootstrap()._runtime_environment()

    assert not any(name.upper().startswith("PYTHON") for name in environment)
    assert environment["CODEXBOT_SENTINEL"] == "preserved"


def test_bootstrap_is_neutral_when_runtime_is_missing_and_never_logs_payload(tmp_path: Path) -> None:
    secret_payload = "完整提示词 secret-bootstrap-value"
    environment = os.environ.copy()
    environment["CODEXBOT_DATA_DIR"] = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "plugin" / "codexbot" / "hooks" / "entry.py")],
        input=secret_payload.encode("utf-8"),
        capture_output=True,
        env=environment,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout.decode("utf-8")) == {}
    log = (tmp_path / "logs" / "bootstrap.log").read_text(encoding="utf-8")
    assert secret_payload not in log
    assert "runtime is not installed" in log


def test_runtime_hook_writes_queue_and_returns_empty_json(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["CODEXBOT_DATA_DIR"] = str(tmp_path)
    environment["CODEXBOT_DISABLE_DAEMON"] = "1"
    environment["PYTHONPATH"] = str(ROOT / "src")
    prompt = "测试任务 api_key=hook-secret-value"
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "session-hook",
        "turn_id": "turn-hook",
        "cwd": str(ROOT),
        "model": "gpt-5.6-codex",
        "prompt": prompt,
    }
    completed = subprocess.run(
        [sys.executable, "-m", "codexbot.hooks"],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=environment,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout.decode("utf-8")) == {}
    assert (tmp_path / "state.sqlite3").is_file()
    disk = "".join(
        path.read_bytes().decode("utf-8", errors="ignore")
        for path in tmp_path.iterdir()
        if path.is_file()
    )
    assert prompt not in disk
    assert "hook-secret-value" not in disk
