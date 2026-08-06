from __future__ import annotations

import os
import subprocess

import codexbot.processes as processes
from codexbot.processes import (
    HostProcess,
    ProcessInfo,
    discover_codex_host,
    discover_running_codex_host,
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


def test_cli_fallback_skips_transient_runner_and_accepts_desktop_snapshot() -> None:
    runner = _process(
        10,
        "codex-command-runner.exe",
        r"C:\Codex\codex-command-runner.exe",
        "codex-command-runner.exe",
    )
    cli = _process(20, "codex.exe", r"C:\Codex\codex.exe", "codex.exe", "exec")
    desktop = _process(
        30,
        "ChatGPT.exe",
        r"C:\Program Files\WindowsApps\OpenAI.Codex_x64\ChatGPT.exe",
        "ChatGPT.exe",
    )

    assert select_codex_host([runner, cli]) == HostProcess(20, 20.0, "cli")
    assert select_codex_host([desktop]) == HostProcess(30, 30.0, "desktop")

    node_runner = _process(
        11,
        "node.exe",
        r"C:\Program Files\nodejs\node.exe",
        "node.exe",
        r"C:\Codex\codex-command-runner.js",
    )
    assert select_codex_host([node_runner, cli]) == HostProcess(20, 20.0, "cli")

    app_server = _process(
        25,
        "codex.exe",
        r"C:\Codex\codex.exe",
        "codex.exe",
        "app-server",
    )
    renderer = _process(
        26,
        "ChatGPT.exe",
        r"C:\Apps\ChatGPT.exe",
        "ChatGPT.exe",
        "--type=renderer",
    )
    assert select_codex_host([app_server, renderer, desktop]) == HostProcess(
        30, 30.0, "desktop"
    )


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


def test_discovery_uses_bootstrap_ancestor_snapshot_after_runner_exits(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    snapshot = [
        _process(
            10,
            "codex-command-runner.exe",
            r"C:\Codex\codex-command-runner.exe",
            "codex-command-runner.exe",
        ),
        _process(20, "codex.exe", r"C:\Codex\codex.exe", "codex.exe", "app-server"),
        _process(
            30,
            "ChatGPT.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_x64\ChatGPT.exe",
            "ChatGPT.exe",
        ),
    ]
    captured: list[int] = []

    def snapshots(pids):  # type: ignore[no-untyped-def]
        captured.extend(pids)
        return snapshot

    monkeypatch.setattr(processes, "_snapshots_for_pids", snapshots)
    monkeypatch.setattr(
        processes.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(OSError("runner already exited")),
    )
    monkeypatch.setattr(processes, "discover_running_codex_host", lambda: None)

    assert discover_codex_host(999, ancestor_pids=(10, 20, 30)) == HostProcess(
        30, 30.0, "desktop"
    )
    assert captured == [10, 20, 30]


def test_global_discovery_prefers_stable_app_server_over_transient_runner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class FakeProcess:
        def __init__(self, item: ProcessInfo) -> None:
            self.item = item
            self.pid = item.pid
            self.info = {"name": item.name}

        def create_time(self) -> float:
            return self.item.create_time

        def name(self) -> str:
            return self.item.name

        def exe(self) -> str:
            return self.item.executable

        def cmdline(self) -> list[str]:
            return list(self.item.command_line)

    runner = _process(
        10,
        "codex-command-runner.exe",
        r"C:\Codex\codex-command-runner.exe",
        "codex-command-runner.exe",
    )
    app_server = _process(
        20,
        "codex.exe",
        r"C:\Codex\codex.exe",
        "codex.exe",
        "app-server",
    )
    inspected_unrelated = False

    class UnrelatedProcess:
        pid = 99
        info = {"name": "unrelated-service.exe"}

        def create_time(self) -> float:
            nonlocal inspected_unrelated
            inspected_unrelated = True
            raise AssertionError("unrelated process should not receive a full snapshot")

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: [
            UnrelatedProcess(),
            FakeProcess(runner),
            FakeProcess(app_server),
        ],
    )

    assert discover_running_codex_host() == HostProcess(20, 20.0, "app-server")
    assert inspected_unrelated is False

    monkeypatch.setattr(
        processes.psutil,
        "process_iter",
        lambda *_args, **_kwargs: [FakeProcess(runner)],
    )
    assert discover_running_codex_host() is None


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


def test_python_distribution_environment_is_removed() -> None:
    environment = isolated_python_environment(
        {
            "PATH": r"C:\Windows",
            "PYTHONHOME": r"C:\Program Files\LibreOffice\program\python-core",
            "PythonPath": r"C:\Program Files\LibreOffice\program",
            "PYTHONUTF8": "1",
            "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        }
    )

    assert environment == {
        "PATH": r"C:\Windows",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
    }


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
    launch_kwargs: dict[str, object] = {}

    def fake_popen(_args: list[str], **kwargs: object) -> Child:
        launch_kwargs.update(kwargs)
        return Child()

    monkeypatch.setattr("codexbot.processes.data_dir", lambda: tmp_path)
    monkeypatch.setattr("codexbot.processes.psutil.Process", BrokenProcess)
    monkeypatch.setattr("codexbot.processes.subprocess.Popen", fake_popen)
    monkeypatch.setattr("codexbot.processes.time.time", lambda: 1234.5)

    assert ensure_daemon(state) is True
    assert state.info == (4321, 1234.5)
    if os.name == "nt":
        assert int(launch_kwargs["creationflags"]) & subprocess.CREATE_NO_WINDOW
        assert int(launch_kwargs["creationflags"]) & subprocess.DETACHED_PROCESS
        assert "startupinfo" in launch_kwargs
