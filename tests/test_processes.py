from __future__ import annotations

from codexbot.processes import (
    HostProcess,
    ProcessInfo,
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
        }
    )

    assert environment == {"PATH": r"C:\Windows"}
