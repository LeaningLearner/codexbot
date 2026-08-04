from __future__ import annotations

import psutil
import pytest

from codexbot.processes import (
    HostProcess,
    ProcessInfo,
    discover_codex_host,
    discover_codex_host_by_scan,
    ensure_daemon,
    isolated_python_environment,
    process_matches,
    select_codex_host,
)


def _process(
    pid: int,
    name: str,
    executable: str,
    *command_line: str,
    created: float | None = None,
) -> ProcessInfo:
    return ProcessInfo(pid, float(created if created is not None else pid), name, executable, command_line)


def test_desktop_host_wins_over_nested_app_server() -> None:
    chain = [
        _process(10, "python.exe", r"C:\Python\python.exe", "python", "entry.py"),
        _process(20, "codex.exe", r"C:\Codex\codex.exe", "codex.exe", "app-server"),
        _process(
            30,
            "ChatGPT.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.2.3_x64\ChatGPT.exe",
            "ChatGPT.exe",
        ),
    ]

    assert select_codex_host(chain) == HostProcess(30, 30.0, "desktop")


def test_cli_and_app_server_fallbacks() -> None:
    app_server = _process(20, "codex.exe", r"C:\Codex\codex.exe", "codex.exe", "app-server")
    assert select_codex_host([app_server]) == HostProcess(20, 20.0, "app-server")

    cli = _process(40, "codex.exe", r"C:\Codex\codex.exe", "codex", "exec")
    assert select_codex_host([cli]) == HostProcess(40, 40.0, "cli")


def test_process_detection_accepts_variant_names_and_paths() -> None:
    app_server = _process(
        21,
        "codex",
        r"D:\Tools\codex-aarch64.exe",
        "codex",
        "--app-server",
    )
    desktop = _process(
        31,
        "ChatGPT.exe",
        r"D:\Apps\ChatGPT\ChatGPT.exe",
        "ChatGPT.exe",
    )
    assert select_codex_host([app_server, desktop]) == HostProcess(31, 31.0, "desktop")

    node_cli = _process(
        41,
        "node.exe",
        r"C:\Program Files\nodejs\node.exe",
        "node",
        r"C:\Tools\codex.js",
        "exec",
    )
    assert select_codex_host([node_cli]) == HostProcess(41, 41.0, "cli")


def test_process_detection_accepts_luna_worker_hosts() -> None:
    worker = _process(
        51,
        "luna_worker.exe",
        r"C:\Codex\luna_worker.exe",
        "luna_worker.exe",
    )

    assert select_codex_host([worker]) == HostProcess(51, 51.0, "cli")


def test_pid_reuse_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def is_running(self) -> bool:
            return True

        def create_time(self) -> float:
            return 200.0

    monkeypatch.setattr("codexbot.processes.psutil.Process", FakeProcess)

    assert process_matches(123, 200.0)
    assert not process_matches(123, 100.0)


class _FakeIterProcess:
    def __init__(self, info: dict[str, object]) -> None:
        self.info = info


def test_discover_codex_host_falls_back_to_scan_when_parent_chain_is_gone(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class MissingProcess:
        def __init__(self, _pid: int) -> None:
            raise psutil.NoSuchProcess(pid=0)

    monkeypatch.setattr("codexbot.processes.psutil.Process", MissingProcess)
    monkeypatch.setattr(
        "codexbot.processes.psutil.process_iter",
        lambda *_args, **_kwargs: iter(
            [
                _FakeIterProcess(
                    {
                        "pid": 100,
                        "create_time": 100.0,
                        "name": "codex.exe",
                        "exe": r"C:\Codex\codex.exe",
                        "cmdline": ["codex.exe", "app-server"],
                    }
                )
            ]
        ),
    )

    assert discover_codex_host() == HostProcess(100, 100.0, "app-server")


def test_scan_prefers_app_server_over_desktop() -> None:
    processes = [
        _FakeIterProcess(
            {
                "pid": 1,
                "create_time": 1.0,
                "name": "codex.exe",
                "exe": r"C:\Codex\codex.exe",
                "cmdline": ["codex.exe", "app-server"],
            }
        ),
        _FakeIterProcess(
            {
                "pid": 2,
                "create_time": 2.0,
                "name": "ChatGPT.exe",
                "exe": r"C:\WindowsApps\OpenAI.Codex_1.2.3_x64\ChatGPT.exe",
                "cmdline": ["ChatGPT.exe"],
            }
        ),
    ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "codexbot.processes.psutil.process_iter",
        lambda *_args, **_kwargs: iter(processes),
    )
    try:
        assert discover_codex_host_by_scan() == HostProcess(1, 1.0, "app-server")
    finally:
        monkeypatch.undo()


def test_scan_uses_desktop_when_no_app_server_exists() -> None:
    processes = [
        _FakeIterProcess(
            {
                "pid": 7,
                "create_time": 7.0,
                "name": "ChatGPT.exe",
                "exe": r"C:\WindowsApps\OpenAI.Codex_1.2.3_x64\ChatGPT.exe",
                "cmdline": ["ChatGPT.exe"],
            }
        )
    ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "codexbot.processes.psutil.process_iter",
        lambda *_args, **_kwargs: iter(processes),
    )
    try:
        assert discover_codex_host_by_scan() == HostProcess(7, 7.0, "desktop")
    finally:
        monkeypatch.undo()


def test_scan_skips_unreadable_processes() -> None:
    class Unreadable:
        @property
        def info(self) -> dict[str, object]:
            raise psutil.AccessDenied(pid=9)

    processes = [
        Unreadable(),
        _FakeIterProcess(
            {
                "pid": 3,
                "create_time": 3.0,
                "name": "ChatGPT.exe",
                "exe": r"C:\WindowsApps\OpenAI.Codex_1.2.3_x64\ChatGPT.exe",
                "cmdline": ["ChatGPT.exe"],
            }
        ),
    ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "codexbot.processes.psutil.process_iter",
        lambda *_args, **_kwargs: iter(processes),
    )
    try:
        assert discover_codex_host_by_scan() == HostProcess(3, 3.0, "desktop")
    finally:
        monkeypatch.undo()


def test_scan_returns_none_without_codex_processes() -> None:
    processes = [
        _FakeIterProcess(
            {
                "pid": 4,
                "create_time": 4.0,
                "name": "explorer.exe",
                "exe": r"C:\Windows\explorer.exe",
                "cmdline": ["explorer.exe"],
            }
        )
    ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "codexbot.processes.psutil.process_iter",
        lambda *_args, **_kwargs: iter(processes),
    )
    try:
        assert discover_codex_host_by_scan() is None
    finally:
        monkeypatch.undo()


def test_python_distribution_environment_is_removed() -> None:
    environment = isolated_python_environment(
        {
            "PATH": r"C:\Windows",
            "PYTHONHOME": r"C:\Program Files\LibreOffice\program\python-core",
            "PythonPath": r"C:\Program Files\LibreOffice\program",
            "PYTHONUTF8": "1",
        }
    )

    assert environment == {"PATH": r"C:\Windows"}


def test_daemon_start_records_fallback_time_when_process_lookup_raises_oserror(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    class State:
        info: tuple[int, float] | None = None

        def get_daemon_info(self) -> tuple[int, float] | None:
            return self.info

        def set_daemon_info(self, pid: int, create_time: float) -> None:
            self.info = (pid, create_time)

    class Child:
        pid = 4321

    class BrokenProcess:
        def __init__(self, _pid: int) -> None:
            raise OSError("process table unavailable")

    state = State()
    monkeypatch.setattr("codexbot.processes.data_dir", lambda: tmp_path)
    monkeypatch.setattr("codexbot.processes.psutil.Process", BrokenProcess)
    monkeypatch.setattr("codexbot.processes.subprocess.Popen", lambda *_args, **_kwargs: Child())
    monkeypatch.setattr("codexbot.processes.time.time", lambda: 1234.5)

    assert ensure_daemon(state) is True
    assert state.info == (4321, 1234.5)
