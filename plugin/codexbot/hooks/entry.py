"""Tiny stdlib-only bootstrap used directly by Codex hooks.

The real handler lives in the isolated CodexBot runtime.  This process never
performs network I/O and always returns neutral JSON so it cannot steer Codex.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


def _data_dir() -> Path:
    override = os.environ.get("CODEXBOT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "CodexBot" if base else Path.home() / "AppData" / "Local" / "CodexBot"


def _record_bootstrap_error(message: str) -> None:
    try:
        log_dir = _data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "bootstrap.log").open("a", encoding="utf-8") as handle:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            handle.write(f"{stamp} {message[:500]}\n")
    except OSError:
        pass


def _runtime_environment() -> dict[str, str]:
    """Drop variables injected by another Python distribution.

    On this machine ``python`` resolves to LibreOffice's bundled interpreter,
    which exports PYTHONHOME/PYTHONPATH before starting this bootstrap.  Passing
    those variables to the CodexBot virtual environment makes it use
    LibreOffice's incomplete standard library instead of Python 3.11's.
    """

    environment = os.environ.copy()
    for name in tuple(environment):
        if name.upper().startswith("PYTHON"):
            environment.pop(name, None)
    return environment


def main() -> int:
    payload = sys.stdin.buffer.read()
    runtime = _data_dir() / "runtime" / "Scripts" / "python.exe"
    try:
        if not runtime.is_file():
            raise FileNotFoundError("CodexBot runtime is not installed; run install.cmd")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            [str(runtime), "-E", "-m", "codexbot.hooks"],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=_runtime_environment(),
            timeout=1.6,
            check=False,
            creationflags=flags,
        )
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            _record_bootstrap_error(f"runtime hook failed ({completed.returncode}): {detail}")
    except Exception as exc:  # Hook failures must never change the Codex turn.
        _record_bootstrap_error(f"{type(exc).__name__}: {exc}")

    sys.stdout.write(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
