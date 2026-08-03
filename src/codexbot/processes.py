from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Iterable, Mapping, Protocol

import psutil

from .locks import FileLock
from .paths import data_dir


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    create_time: float
    name: str
    executable: str
    command_line: tuple[str, ...]


@dataclass(frozen=True)
class HostProcess:
    pid: int
    create_time: float
    kind: str


class DaemonState(Protocol):
    def get_daemon_info(self) -> tuple[int, float] | None: ...

    def set_daemon_info(self, pid: int, create_time: float) -> None: ...


def isolated_python_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Return an environment safe for launching the isolated runtime."""

    return {
        name: value
        for name, value in source.items()
        if not name.upper().startswith("PYTHON")
    }


def _snapshot(process: psutil.Process) -> ProcessInfo | None:
    try:
        return ProcessInfo(
            pid=process.pid,
            create_time=process.create_time(),
            name=process.name(),
            executable=process.exe(),
            command_line=tuple(process.cmdline()),
        )
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return None


def select_codex_host(chain: Iterable[ProcessInfo]) -> HostProcess | None:
    items = list(chain)
    app_server: ProcessInfo | None = None
    for item in items:
        name = item.name.casefold()
        command = " ".join(item.command_line).casefold()
        if name == "codex.exe" and "app-server" in command:
            app_server = item
            break

    if app_server is not None:
        try:
            index = items.index(app_server)
        except ValueError:
            index = -1
        for item in items[index + 1 :]:
            executable = item.executable.casefold()
            if item.name.casefold() == "chatgpt.exe" and "openai.codex_" in executable:
                return HostProcess(item.pid, item.create_time, "desktop")
        return HostProcess(app_server.pid, app_server.create_time, "app-server")

    for item in items:
        name = item.name.casefold()
        command = " ".join(item.command_line).casefold()
        if (name in {"codex", "codex.exe"} or name.startswith("codex-")) and "codexbot" not in command:
            return HostProcess(item.pid, item.create_time, "cli")
    return None


def discover_codex_host(start_pid: int | None = None) -> HostProcess | None:
    try:
        process = psutil.Process(start_pid or os.getpid())
        chain: list[ProcessInfo] = []
        current: psutil.Process | None = process
        while current is not None:
            item = _snapshot(current)
            if item is not None:
                chain.append(item)
            try:
                current = current.parent()
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                break
        return select_codex_host(chain)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None


def process_matches(pid: int, create_time: float, *, tolerance: float = 0.25) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - create_time) <= tolerance
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def ensure_daemon(state: DaemonState) -> bool:
    info = state.get_daemon_info()
    if info and process_matches(*info):
        return False

    lock = FileLock(data_dir() / "daemon-start.lock", timeout=0.6)
    if not lock.acquire():
        return False
    try:
        info = state.get_daemon_info()
        if info and process_matches(*info):
            return False

        executable = Path(sys.executable)
        if os.name == "nt":
            windowed = executable.with_name("pythonw.exe")
            if windowed.is_file():
                executable = windowed

        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        environment = isolated_python_environment(os.environ)
        environment["CODEXBOT_DATA_DIR"] = str(data_dir())
        child = subprocess.Popen(
            [str(executable), "-E", "-m", "codexbot.daemon"],
            cwd=str(data_dir()),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        try:
            created = psutil.Process(child.pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            created = time.time()
        state.set_daemon_info(child.pid, created)
        return True
    finally:
        lock.release()
