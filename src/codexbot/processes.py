from __future__ import annotations

from dataclasses import dataclass
import ntpath
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


def _process_stem(value: str) -> str:
    basename = ntpath.basename(value.replace("/", "\\")).casefold()
    stem, suffix = ntpath.splitext(basename)
    if suffix in {".exe", ".cmd", ".bat", ".com", ".js"}:
        return stem
    return basename


def _looks_like_codex_name(value: str) -> bool:
    stem = _process_stem(value)
    return stem == "codex" or stem.startswith(("codex-", "codex_"))


def _looks_like_codex_process(item: ProcessInfo) -> bool:
    if _looks_like_codex_name(item.name) or _looks_like_codex_name(item.executable):
        return True
    # Node-based/global CLI installations may report node.exe as the process
    # name while the first command-line script is codex.js.
    return any(_looks_like_codex_name(argument) for argument in item.command_line[:3])


def _has_app_server_argument(item: ProcessInfo) -> bool:
    for argument in item.command_line:
        normalized = ntpath.basename(argument.replace("/", "\\")).casefold()
        normalized = normalized.lstrip("-/\\").split("=", 1)[0]
        if normalized in {"app-server", "app_server"}:
            return True
    return False


def _is_app_server(item: ProcessInfo) -> bool:
    return _looks_like_codex_process(item) and _has_app_server_argument(item)


def _is_desktop_host(item: ProcessInfo) -> bool:
    stems = {_process_stem(item.name), _process_stem(item.executable)}
    if any(
        stem == "chatgpt"
        or stem.startswith(("chatgpt-", "chatgpt_"))
        or stem in {"codexdesktop", "codex-desktop", "codex_desktop"}
        or ("codex" in stem and "desktop" in stem)
        for stem in stems
    ):
        return True

    for argument in item.command_line:
        stem = _process_stem(argument)
        if stem in {"chatgpt", "codexdesktop", "codex-desktop", "codex_desktop"}:
            return True
    return False


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
    """Select the nearest useful Codex host from a child-to-parent chain.

    Codex CLI distributions and desktop package paths vary. Identification is
    based on executable/command semantics and the parent relationship, rather
    than one installed WindowsApps path.
    """

    items = list(chain)
    app_server: ProcessInfo | None = next(
        (item for item in items if _is_app_server(item)),
        None,
    )

    if app_server is not None:
        index = items.index(app_server)
        for item in items[index + 1 :]:
            if _is_desktop_host(item):
                return HostProcess(item.pid, item.create_time, "desktop")
        return HostProcess(app_server.pid, app_server.create_time, "app-server")

    for item in items:
        if _looks_like_codex_process(item):
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
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
                break
        return select_codex_host(chain)
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return None


def process_matches(pid: int, create_time: float, *, tolerance: float = 0.25) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - create_time) <= tolerance
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return False


def ensure_daemon(state: DaemonState, *, standalone: bool = False) -> bool:
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
        environment["CODEXBOT_STANDALONE"] = "1" if standalone else "0"
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
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            created = time.time()
        state.set_daemon_info(child.pid, created)
        return True
    finally:
        lock.release()
