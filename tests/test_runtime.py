from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import codexbot.daemon as daemon
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
    runtime.hostless_startup_grace = 0

    await runtime.monitor_hosts()

    assert runtime.stop_event.is_set()
    assert store.list_hosts() == []


@pytest.mark.asyncio
async def test_hostless_runtime_waits_for_qq_ready_and_delivers_pending_reply(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    store.set_setting("bound_openid", "owner")
    store.ingest_hook(
        {
            "hook_event_name": "Stop",
            "session_id": "hostless-session",
            "turn_id": "hostless-turn",
            "cwd": str(tmp_path),
            "model": "model",
            "last_assistant_message": "reply queued after the hook runner exited",
        }
    )
    messages: list[str] = []

    class FakeApi:
        async def post_c2c_message(self, **kwargs: object) -> object:
            messages.append(str(kwargs["content"]))
            return object()

    class FakeClient:
        def __init__(self, runtime: QQRuntime) -> None:
            self.runtime = runtime
            self.api = FakeApi()
            self.closed = False

        async def start(self, **_kwargs: object) -> None:
            # Real QQ startup took about three seconds in the captured logs;
            # this scaled delay reproduces the same monitor race.
            await asyncio.sleep(0.03)
            await self.runtime.on_ready()
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    clients: list[FakeClient] = []

    def make_client(runtime: QQRuntime) -> FakeClient:
        runtime.monitor_interval = 0.005
        runtime.empty_host_checks = 2
        runtime.hostless_startup_grace = 0.1
        client = FakeClient(runtime)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    await asyncio.wait_for(
        run_qq_runtime(
            store,
            Credentials("appid", "secret"),
            logging.getLogger("test-hostless-delivery"),
            initial_reconnect_delay=0,
        ),
        timeout=2,
    )

    assert len(clients) == 1
    assert clients[0].closed
    assert len(messages) == 1
    assert "reply queued after the hook runner exited" in messages[0]
    assert store.get_due_outbox() is None


@pytest.mark.asyncio
async def test_hostless_pending_reply_survives_default_reconnect_delay(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    store.set_setting("bound_openid", "owner")
    store.ingest_hook(
        {
            "hook_event_name": "Stop",
            "session_id": "reconnect-session",
            "turn_id": "reconnect-turn",
            "cwd": str(tmp_path),
            "model": "model",
            "last_assistant_message": "deliver after the default reconnect delay",
        }
    )
    messages: list[str] = []
    clients: list[FakeClient] = []

    class FakeApi:
        def __init__(self, runtime: QQRuntime, number: int) -> None:
            self.runtime = runtime
            self.number = number

        async def post_c2c_message(self, **kwargs: object) -> object:
            if self.number == 1:
                raise ConnectionError("first websocket disconnected")
            messages.append(str(kwargs["content"]))
            self.runtime.stop_event.set()
            return object()

    class FakeClient:
        def __init__(self, runtime: QQRuntime, number: int) -> None:
            self.runtime = runtime
            self.number = number
            self.api = FakeApi(runtime, number)
            self.closed = False

        async def start(self, **_kwargs: object) -> None:
            await self.runtime.on_ready()
            if self.number == 1:
                raise ConnectionError("first websocket disconnected")
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    def make_client(runtime: QQRuntime) -> FakeClient:
        runtime.monitor_interval = 0.005
        runtime.empty_host_checks = 2
        runtime.hostless_startup_grace = 0.05
        client = FakeClient(runtime, len(clients) + 1)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    await asyncio.wait_for(
        run_qq_runtime(
            store,
            Credentials("appid", "secret"),
            logging.getLogger("test-hostless-reconnect"),
        ),
        timeout=10,
    )

    assert len(clients) == 2
    assert all(client.closed for client in clients)
    assert len(messages) == 1
    assert "deliver after the default reconnect delay" in messages[0]
    assert store.has_pending_outbox() is False


@pytest.mark.asyncio
async def test_host_monitor_standalone_stays_online(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
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
    runtime = QQRuntime(store, logging.getLogger("test-host-monitor"), standalone=True)
    runtime.monitor_interval = 0.001
    runtime.empty_host_checks = 2

    task = asyncio.create_task(runtime.monitor_hosts())
    await asyncio.sleep(0.02)
    assert not runtime.stop_event.is_set()
    assert store.list_hosts() == []  # dead hosts are still pruned
    runtime.stop_event.set()
    await task


@pytest.mark.asyncio
async def test_host_monitor_stays_online_for_active_pairing(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.create_pairing("123456", time.time() + 60)
    runtime = QQRuntime(store, logging.getLogger("test-pairing-monitor"))
    runtime.monitor_interval = 0.001
    runtime.empty_host_checks = 2
    runtime.hostless_startup_grace = 0
    runtime.ready_event.set()

    task = asyncio.create_task(runtime.monitor_hosts())
    await asyncio.sleep(0.02)
    assert not runtime.stop_event.is_set()
    runtime.stop_event.set()
    await task


def test_daemon_final_handoff_restarts_after_clearing_old_pid(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    events: list[str] = []

    class FakeSingleton:
        def acquire(self) -> bool:
            events.append("acquire")
            return True

        def release(self) -> None:
            events.append("release")

    class FakeStore:
        def set_daemon_info(self, _pid: int, _created: float) -> None:
            events.append("set")

        def clear_daemon_info(self, _pid: int) -> None:
            events.append("clear")

        def cleanup(self) -> None:
            return None

        def companion_work_pending(self) -> bool:
            assert events[-2:] == ["clear", "release"]
            return True

        def list_hosts(self) -> list[object]:
            return []

    class FakeProcess:
        def __init__(self, _pid: int) -> None:
            pass

        def create_time(self) -> float:
            return 123.0

    fake_store = FakeStore()

    async def no_runtime(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(daemon, "ensure_data_dir", lambda: tmp_path)
    monkeypatch.setattr(daemon, "database_path", lambda: tmp_path / "state.sqlite3")
    monkeypatch.setattr(daemon, "FileLock", lambda *_args, **_kwargs: FakeSingleton())
    monkeypatch.setattr(daemon, "Store", lambda _path: fake_store)
    monkeypatch.setattr(daemon, "configure_logging", lambda _name: logging.getLogger("handoff"))
    monkeypatch.setattr(daemon, "_run_active_daemon", no_runtime)
    monkeypatch.setattr(daemon.psutil, "Process", FakeProcess)

    def restart(state: object) -> bool:
        assert state is fake_store
        events.append("restart")
        return True

    monkeypatch.setattr(daemon, "ensure_daemon", restart)

    assert daemon.main() == 0
    assert events[-3:] == ["clear", "release", "restart"]


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
async def test_runtime_reconnects_when_delivery_loop_exits(tmp_path: Path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
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
                await asyncio.Future()
            self.runtime.stop_event.set()
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    async def broken_delivery(self: QQRuntime, client: FakeClient) -> None:
        await self.ready_event.wait()
        if client.number == 1:
            raise RuntimeError("delivery api_key=delivery-secret-value")
        await self.stop_event.wait()

    def make_client(runtime: QQRuntime) -> FakeClient:
        client = FakeClient(runtime, len(clients) + 1)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client.QQRuntime.delivery_loop", broken_delivery)
    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    with caplog.at_level(logging.ERROR, logger="test-delivery-failure"):
        await run_qq_runtime(
            store,
            Credentials("appid", "secret"),
            logging.getLogger("test-delivery-failure"),
            initial_reconnect_delay=0,
        )

    assert len(clients) == 2
    assert all(client.closed for client in clients)
    assert "QQ delivery loop failed" in caplog.text
    assert "delivery-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_runtime_stops_when_host_monitor_fails(tmp_path: Path, monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    clients: list[FakeClient] = []

    class FakeApi:
        async def post_c2c_message(self, **_kwargs: object) -> object:
            return object()

    class FakeClient:
        def __init__(self, runtime: QQRuntime) -> None:
            self.runtime = runtime
            self.api = FakeApi()
            self.closed = False

        async def start(self, **_kwargs: object) -> None:
            await self.runtime.on_ready()
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    async def broken_monitor(self: QQRuntime) -> None:
        raise RuntimeError("monitor Bearer monitor-secret-value")

    def make_client(runtime: QQRuntime) -> FakeClient:
        client = FakeClient(runtime)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client.QQRuntime.monitor_hosts", broken_monitor)
    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    with caplog.at_level(logging.ERROR, logger="test-monitor-failure"):
        await run_qq_runtime(
            store,
            Credentials("appid", "secret"),
            logging.getLogger("test-monitor-failure"),
            initial_reconnect_delay=0,
        )

    assert len(clients) == 1
    assert clients[0].closed
    assert "Codex host monitor failed" in caplog.text
    assert "monitor-secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_daemon_waits_for_credentials_to_appear(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "state.sqlite3")
    credentials = Credentials("appid", "secret")
    values = iter([None, credentials])

    monkeypatch.setattr(daemon, "load_credentials", lambda: next(values))

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(daemon.asyncio, "sleep", no_sleep)

    result = await daemon._wait_without_credentials(
        store,
        logging.getLogger("test-credentials"),
        poll_interval=0,
    )

    assert result == credentials


@pytest.mark.asyncio
async def test_periodic_cleanup_runs_in_a_worker_thread(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite3")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls: list[int] = []
    main_thread = threading.get_ident()

    def cleanup() -> None:
        calls.append(threading.get_ident())
        loop.call_soon_threadsafe(stop_event.set)

    store.cleanup = cleanup  # type: ignore[method-assign]
    await asyncio.wait_for(
        daemon._periodic_cleanup(
            store,
            logging.getLogger("test-periodic-cleanup"),
            stop_event,
            interval=0,
        ),
        timeout=2,
    )

    assert calls
    assert calls[0] != main_thread


@pytest.mark.asyncio
async def test_wait_without_credentials_treats_process_oserror_as_dead(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    store = Store(tmp_path / "state.sqlite3")
    store.ingest_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-oserror",
            "cwd": str(tmp_path),
            "model": "model",
        },
        HostProcess(123, 456.0, "desktop"),
    )
    values = iter([None, None])
    monkeypatch.setattr(daemon, "load_credentials", lambda: next(values))

    class BrokenProcess:
        def __init__(self, _pid: int) -> None:
            raise OSError("process lookup failed")

    monkeypatch.setattr(daemon.psutil, "Process", BrokenProcess)

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(daemon.asyncio, "sleep", no_sleep)
    result = await daemon._wait_without_credentials(store, logging.getLogger("test-oserror"))

    assert result is None
    assert store.list_hosts() == []


@pytest.mark.asyncio
async def test_runtime_drains_outbox_before_closing_client(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "state.sqlite3")
    store.set_setting("bound_openid", "owner")
    store.ingest_hook(
        {
            "hook_event_name": "Stop",
            "session_id": "session-drain",
            "turn_id": "turn-drain",
            "cwd": str(tmp_path),
            "model": "model",
            "last_assistant_message": "final reply before shutdown",
        }
    )
    messages: list[str] = []

    class FakeApi:
        async def post_c2c_message(self, **kwargs: object) -> object:
            messages.append(str(kwargs["content"]))
            return object()

    class FakeClient:
        def __init__(self, runtime: QQRuntime) -> None:
            self.runtime = runtime
            self.api = FakeApi()
            self.closed = False

        async def start(self, **_kwargs: object) -> None:
            await self.runtime.on_ready()
            self.runtime.stop_event.set()
            await asyncio.Future()

        async def close(self) -> None:
            self.closed = True

        def is_closed(self) -> bool:
            return self.closed

    clients: list[FakeClient] = []

    def make_client(runtime: QQRuntime) -> FakeClient:
        client = FakeClient(runtime)
        clients.append(client)
        return client

    monkeypatch.setattr("codexbot.qq_client._make_client", make_client)
    await run_qq_runtime(
        store,
        Credentials("appid", "secret"),
        logging.getLogger("test-drain"),
        initial_reconnect_delay=0.01,
    )

    assert len(clients) == 1
    assert clients[0].closed
    assert len(messages) == 1
    assert "final reply before shutdown" in messages[0]
    assert store.get_due_outbox() is None


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


@pytest.mark.asyncio
async def test_login_completion_sender_follows_reconnected_client(tmp_path: Path) -> None:
    runtime = QQRuntime(Store(tmp_path / "state.sqlite3"), logging.getLogger("test-reconnect-send"))
    captured: dict[str, object] = {}

    class Commands:
        async def handle(self, **kwargs: object) -> str:
            captured["active_send"] = kwargs["active_send"]
            return "captured"

    class Api:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def post_c2c_message(self, **kwargs: object) -> object:
            self.messages.append(str(kwargs["content"]))
            return object()

    class Client:
        def __init__(self) -> None:
            self.api = Api()
            self.closed = False

        def is_closed(self) -> bool:
            return self.closed

    class Author:
        user_openid = "owner"

    class Message:
        author = Author()
        id = "login-command"
        content = "/status"
        _api = Api()

    runtime.commands = Commands()  # type: ignore[assignment]
    original = Client()
    replacement = Client()
    runtime.current_client = original
    await runtime.on_c2c_message(original, Message())

    runtime.current_client = replacement
    active_send = captured["active_send"]
    assert callable(active_send)
    await active_send("owner", "login complete")

    assert original.api.messages == []
    assert replacement.api.messages == ["login complete"]


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
    # Pythonw and Windows Credential Manager cold starts vary substantially on
    # a busy desktop. The companion must still leave promptly after two empty
    # host polls, but allow headroom for process initialization.
    deadline = started + 12.0
    while time.monotonic() < deadline and store.get_daemon_info() is not None:
        time.sleep(0.05)

    assert store.get_daemon_info() is None
    assert time.monotonic() - started <= 10.0

    process_deadline = time.monotonic() + 2.0
    while time.monotonic() < process_deadline and process_matches(*info):
        time.sleep(0.05)
    assert not process_matches(*info)
