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
from .subprocess_utils import hidden_console_subprocess_kwargs


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
        or name.upper() == "PYTHON_KEYRING_BACKEND"
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


def _looks_like_desktop_name(value: str) -> bool:
    stem = _process_stem(value)
    return (
        stem == "chatgpt"
        or stem.startswith(("chatgpt-", "chatgpt_"))
        or stem in {"codexdesktop", "codex-desktop", "codex_desktop"}
        or ("codex" in stem and "desktop" in stem)
    )


def _is_desktop_host(item: ProcessInfo) -> bool:
    if _looks_like_desktop_name(item.name) or _looks_like_desktop_name(item.executable):
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


def _snapshots_for_pids(pids: Iterable[int]) -> list[ProcessInfo]:
    snapshots: list[ProcessInfo] = []
    seen: set[int] = set()
    for raw_pid in pids:
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        try:
            item = _snapshot(psutil.Process(pid))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            item = None
        if item is not None:
            snapshots.append(item)
    return snapshots


def _is_transient_codex_helper(item: ProcessInfo) -> bool:
    values = (
        _process_stem(item.name),
        _process_stem(item.executable),
        *(argument.casefold() for argument in item.command_line),
    )
    markers = ("command-runner", "code-mode-host", "sandbox", "apply-patch")
    return any(marker in value for value in values for marker in markers)


def _is_transient_desktop_helper(item: ProcessInfo) -> bool:
    markers = ("--type=", "crashpad", "notification-helper")
    return any(
        marker in argument.casefold()
        for argument in item.command_line
        for marker in markers
    )


def _is_global_discovery_candidate(process: psutil.Process) -> bool:
    try:
        cached = getattr(process, "info", None)
        name = str(cached.get("name") or "") if isinstance(cached, dict) else ""
        if not name:
            name = process.name()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        return False
    stem = _process_stem(name)
    return (
        _looks_like_codex_name(name)
        or _looks_like_desktop_name(name)
        or stem in {"node", "nodejs"}
    )


def discover_running_codex_host() -> HostProcess | None:
    """Find a stable Codex process when a short-lived hook parent disappeared."""

    snapshots: list[ProcessInfo] = []
    try:
        # Request only the cheap process name up front. Reading exe/cmdline for
        # every protected Windows service can block a hook worker for tens of
        # seconds, so take complete snapshots only for plausible candidates.
        processes = psutil.process_iter(["name"])
    except (psutil.AccessDenied, OSError):
        return None
    for process in processes:
        if not _is_global_discovery_candidate(process):
            continue
        item = _snapshot(process)
        if item is not None:
            snapshots.append(item)

    desktop_candidates = [
        item
        for item in snapshots
        if _is_desktop_host(item) and not _is_transient_desktop_helper(item)
    ]
    if desktop_candidates:
        # Electron's main process is normally the oldest stable ChatGPT
        # process; renderer/utility helpers are filtered above.
        desktop = min(desktop_candidates, key=lambda item: item.create_time)
        return HostProcess(desktop.pid, desktop.create_time, "desktop")

    # The app-server is the most precise long-lived Desktop marker. Avoid
    # selecting command-runner/code-mode helpers that vanish after each hook.
    app_server = next((item for item in snapshots if _is_app_server(item)), None)
    if app_server is not None:
        return HostProcess(app_server.pid, app_server.create_time, "app-server")
    cli = next(
        (
            item
            for item in snapshots
            if _looks_like_codex_process(item) and not _is_transient_codex_helper(item)
        ),
        None,
    )
    if cli is not None:
        return HostProcess(cli.pid, cli.create_time, "cli")
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
            if _is_desktop_host(item) and not _is_transient_desktop_helper(item):
                return HostProcess(item.pid, item.create_time, "desktop")
        return HostProcess(app_server.pid, app_server.create_time, "app-server")

    desktop = next(
        (
            item
            for item in items
            if _is_desktop_host(item) and not _is_transient_desktop_helper(item)
        ),
        None,
    )
    if desktop is not None:
        return HostProcess(desktop.pid, desktop.create_time, "desktop")

    for item in items:
        if _looks_like_codex_process(item) and not _is_transient_codex_helper(item):
            return HostProcess(item.pid, item.create_time, "cli")
    return None


def discover_codex_host(
    start_pid: int | None = None,
    *,
    ancestor_pids: Iterable[int] = (),
) -> HostProcess | None:
    ancestor_host = select_codex_host(_snapshots_for_pids(ancestor_pids))
    if ancestor_host is not None:
        return ancestor_host
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
        host = select_codex_host(chain)
        if host is not None:
            return host
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
        pass
    return discover_running_codex_host()


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

        launch_options = hidden_console_subprocess_kwargs(new_process_group=True)
        if os.name == "nt":
            launch_options["creationflags"] = (
                int(launch_options.get("creationflags", 0))
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
            **launch_options,
        )
        try:
            created = psutil.Process(child.pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            created = time.time()
        state.set_daemon_info(child.pid, created)
        return True
    finally:
        lock.release()
