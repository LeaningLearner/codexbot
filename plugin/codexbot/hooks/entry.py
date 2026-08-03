"""Tiny stdlib-only bootstrap used directly by Codex hooks.

The real handler lives in the isolated CodexBot runtime.  This process never
performs network I/O and always returns neutral JSON so it cannot steer Codex.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


BOOTSTRAP_LOG_MAX_BYTES = 256 * 1024
BOOTSTRAP_PAYLOAD_MAX_BYTES = 128 * 1024


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
        log_path = log_dir / "bootstrap.log"
        if log_path.is_file() and log_path.stat().st_size >= BOOTSTRAP_LOG_MAX_BYTES:
            backup_path = log_dir / "bootstrap.log.1"
            try:
                backup_path.unlink(missing_ok=True)
                log_path.replace(backup_path)
            except OSError:
                # A concurrent hook may be rotating the same small diagnostic log.
                pass
        with log_path.open("a", encoding="utf-8") as handle:
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


def _runtime_creation_flags() -> int:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        # The Codex hook command has a short lifetime.  Keep the actual event
        # worker independent from that command so a hook timeout cannot kill
        # it halfway through an SQLite write.
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    return flags


def _launch_runtime(runtime: Path, payload: bytes) -> None:
    if len(payload) > BOOTSTRAP_PAYLOAD_MAX_BYTES:
        raise ValueError("runtime hook payload exceeds the bootstrap limit")
    process = subprocess.Popen(
        [str(runtime), "-E", "-m", "codexbot.hooks"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_runtime_environment(),
        close_fds=True,
        creationflags=_runtime_creation_flags(),
    )
    if process.stdin is None:
        raise RuntimeError("runtime hook stdin was not created")
    try:
        process.stdin.write(payload)
    finally:
        process.stdin.close()


def _read_bounded_payload() -> bytes | None:
    payload = sys.stdin.buffer.read(BOOTSTRAP_PAYLOAD_MAX_BYTES + 1)
    if len(payload) > BOOTSTRAP_PAYLOAD_MAX_BYTES:
        _record_bootstrap_error(
            f"bootstrap payload exceeded {BOOTSTRAP_PAYLOAD_MAX_BYTES} bytes"
        )
        return None
    return payload


def main() -> int:
    try:
        payload = _read_bounded_payload()
        if payload is not None:
            runtime = _data_dir() / "runtime" / "Scripts" / "python.exe"
            if not runtime.is_file():
                raise FileNotFoundError("CodexBot runtime is not installed; run install.cmd")
            _launch_runtime(runtime, payload)
    except Exception as exc:  # Hook failures must never change the Codex turn.
        _record_bootstrap_error(f"{type(exc).__name__}: {exc}")

    sys.stdout.write(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
