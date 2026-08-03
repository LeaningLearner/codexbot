from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from codexbot.processes import HostProcess
from codexbot.processes import process_matches
from codexbot.qq_client import QQRuntime, run_qq_runtime
from codexbot.security import Credentials
from codexbot.store import Store


@pytest.mark.asyncio
async def test_host_monitor_requires_two_empty_checks(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-1",
            "cwd": str(tmp_path),
            "model": "model",
        },
        HostProcess(123, 456.0, "desktop"),
    )
    monkeypatch.setattr("codexbot.qq_client.process_matches", lambda _pid, _time: False)
    runtime = QQRuntime(store, logging.getLogger("test-host-monitor"))
    runtime.monitor_interval = 0
    runtime.empty_host_checks = 2

    await runtime.monitor_hosts()

    assert runtime.stop_event.is_set()
    assert store.list_hosts() == []


@pytest.mark.asyncio
async def test_runtime_reconnects_after_disconnect(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    clients: list[FakeClient] = []

    class FakeApi:
        async def post_c2c_message(self, **_kwargs: object) -> object:
            return object()

    class FakeClient:
        def __init__(self, runtime: QQRuntime, number: int) -> None:
            self.runtime = runtime
            self.number = number
            self.api = FakeApi()
            self.closed = False

        async def start(self, **_kwargs: object) -> None:
            await self.runtime.on_ready()
            if self.number == 1:
                raise ConnectionError("simulated websocket disconnect")
            await asyncio.sleep(0.02)
            self.runtime.stop_event.set()
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    def make_client(runtime: QQRuntime) -> FakeClient:
        client = FakeClient(runtime, len(clients) + 1)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    await run_qq_runtime(
        store,
        Credentials("appid", "secret"),
        logging.getLogger("test-reconnect"),
        initial_reconnect_delay=0.01,
    )

    assert len(clients) == 2
    assert all(client.closed for client in clients)


@pytest.mark.asyncio
async def test_c2c_adapter_matches_botpy_115_without_msg_seq(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.set_setting("bound_openid", "owner")
    runtime = QQRuntime(store, logging.getLogger("test-c2c-adapter"))
    calls: list[dict[str, object]] = []

    class Api:
        async def post_c2c_message(
            self,
            *,
            openid: str,
            msg_type: int,
            content: str,
            msg_id: str | None = None,
        ) -> object:
            calls.append(
                {"openid": openid, "msg_type": msg_type, "content": content, "msg_id": msg_id}
            )
            return object()

    class Author:
        user_openid = "owner"

    class Message:
        author = Author()
        id = "message-id"
        content = "/help"
        _api = Api()

    class Client:
        api = Api()

    await runtime.on_c2c_message(Client(), Message())

    assert len(calls) == 1
    assert calls[0]["msg_id"] == "message-id"
    assert "/status" in str(calls[0]["content"])


@pytest.mark.skipif(os.name != "nt", reason="v1 companion lifecycle is Windows-only")
def test_real_hidden_companion_exits_after_registered_host(tmp_path: Path) -> None:
    data = tmp_path / "companion-data"
    environment = os.environ.copy()
    environment["CODEXBOT_DATA_DIR"] = str(data)
    environment["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    script = """
import os
from pathlib import Path
import psutil
from codexbot.processes import HostProcess, ensure_daemon
from codexbot.store import Store

store = Store(Path(os.environ["CODEXBOT_DATA_DIR"]) / "state.sqlite3")
host = HostProcess(os.getpid(), psutil.Process().create_time(), "cli")
store.ingest_hook(
    {
        "hook_event_name": "SessionStart",
        "session_id": "real-process-smoke",
        "cwd": os.getcwd(),
        "model": "test-model",
        "source": "startup",
    },
    host,
)
print(ensure_daemon(store))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "True"

    store = Store(data / "state.sqlite3")
    info = store.get_daemon_info()
    assert info is not None
    assert process_matches(*info)

    started = time.monotonic()
    deadline = started + 4.0
    while time.monotonic() < deadline and store.get_daemon_info() is not None:
        time.sleep(0.05)

    assert store.get_daemon_info() is None
    assert time.monotonic() - started <= 3.0

    process_deadline = time.monotonic() + 2.0
    while time.monotonic() < process_deadline and process_matches(*info):
        time.sleep(0.05)
    assert not process_matches(*info)
