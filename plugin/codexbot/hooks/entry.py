"""Tiny stdlib-only bootstrap used directly by Codex hooks.

The real handler lives in the isolated CodexBot runtime.  This process never
performs network I/O and always returns neutral JSON so it cannot steer Codex.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys


BOOTSTRAP_LOG_MAX_BYTES = 256 * 1024
BOOTSTRAP_PAYLOAD_MAX_BYTES = 128 * 1024
ANCESTOR_PIDS_ENV = "CODEXBOT_HOOK_ANCESTOR_PIDS"
MAX_ANCESTOR_DEPTH = 32


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


def _ancestor_process_ids() -> tuple[int, ...]:
    """Snapshot the bootstrap's ancestors before short-lived runners exit."""

    immediate_parent = os.getppid()
    if os.name != "nt":
        return (immediate_parent,) if immediate_parent > 0 else ()

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snapshot = kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        create_snapshot.restype = wintypes.HANDLE
        process_first = kernel32.Process32FirstW
        process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
        process_first.restype = wintypes.BOOL
        process_next = kernel32.Process32NextW
        process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
        process_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot in (None, invalid_handle):
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
        parents: dict[int, int] = {}
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            available = bool(process_first(snapshot, ctypes.byref(entry)))
            while available:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                available = bool(process_next(snapshot, ctypes.byref(entry)))
        finally:
            close_handle(snapshot)

        ancestors: list[int] = []
        seen = {os.getpid()}
        current = os.getpid()
        for _ in range(MAX_ANCESTOR_DEPTH):
            parent = parents.get(current)
            if not parent or parent in seen:
                break
            ancestors.append(parent)
            seen.add(parent)
            current = parent
        if ancestors:
            return tuple(ancestors)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return (immediate_parent,) if immediate_parent > 0 else ()


def _runtime_environment() -> dict[str, str]:
    """Drop variables injected by another Python distribution.

    On this machine ``python`` resolves to LibreOffice's bundled interpreter,
    which exports PYTHONHOME/PYTHONPATH before starting this bootstrap.  Passing
    those variables to the CodexBot virtual environment makes it use
    LibreOffice's incomplete standard library instead of Python 3.11's.
    """

    environment = os.environ.copy()
    for name in tuple(environment):
        if name.upper().startswith("PYTHON") and name.upper() != "PYTHON_KEYRING_BACKEND":
            environment.pop(name, None)
    ancestors = _ancestor_process_ids()
    if ancestors:
        environment[ANCESTOR_PIDS_ENV] = ",".join(str(pid) for pid in ancestors)
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


def _runtime_startup_info() -> object | None:
    """Hide the worker even when Windows routes a launch through a wrapper."""

    if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def _launch_runtime(runtime: Path, payload: bytes) -> None:
    if len(payload) > BOOTSTRAP_PAYLOAD_MAX_BYTES:
        raise ValueError("runtime hook payload exceeds the bootstrap limit")
    kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": _runtime_environment(),
        "close_fds": True,
        "creationflags": _runtime_creation_flags(),
    }
    startupinfo = _runtime_startup_info()
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    process = subprocess.Popen(
        [str(runtime), "-E", "-m", "codexbot.hooks"],
        **kwargs,
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


def _runtime_path() -> Path:
    if len(sys.argv) > 2 and sys.argv[1] == "--runtime" and sys.argv[2]:
        return Path(sys.argv[2])
    return _data_dir() / "runtime" / "Scripts" / "python.exe"


def main() -> int:
    try:
        payload = _read_bounded_payload()
        if payload is not None:
            runtime = _runtime_path()
            if not runtime.is_file():
                raise FileNotFoundError("CodexBot runtime is not installed; run install.cmd")
            _launch_runtime(runtime, payload)
    except Exception as exc:  # Hook failures must never change the Codex turn.
        _record_bootstrap_error(f"{type(exc).__name__}: {exc}")

    sys.stdout.write(json.dumps({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
