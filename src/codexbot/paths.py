from __future__ import annotations

import os
from pathlib import Path


APP_DIR_NAME = "CodexBot"


def data_dir() -> Path:
    override = os.environ.get("CODEXBOT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME

    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def ensure_data_dir() -> Path:
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return data_dir() / "state.sqlite3"


def runtime_dir() -> Path:
    return data_dir() / "runtime"


def runtime_python(*, windowed: bool = False) -> Path:
    executable = "pythonw.exe" if windowed else "python.exe"
    return runtime_dir() / "Scripts" / executable


def logs_dir() -> Path:
    return data_dir() / "logs"

