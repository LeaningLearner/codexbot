from __future__ import annotations

from collections import deque
import json
import threading
import time
from typing import Any

import pytest

from codexbot.codex_login import (
    AccountInfo,
    CodexAppServerClient,
    CodexLoginService,
    DeviceLoginResult,
    LoginInProgress,
)


class FakeStdout:
    def __init__(self) -> None:
        self._lines: deque[str] = deque()
        self._condition = threading.Condition()
        self.closed = False
        self.active_readers = 0
        self.max_active_readers = 0

    def push(self, payload: dict[str, Any]) -> None:
        with self._condition:
            self._lines.append(json.dumps(payload) + "\n")
            self._condition.notify_all()

    def readline(self) -> str:
        with self._condition:
            self.active_readers += 1
            self.max_active_readers = max(self.max_active_readers, self.active_readers)
            try:
                while not self._lines and not self.closed:
                    self._condition.wait()
                if self._lines:
                    return self._lines.popleft()
                return ""
            finally:
                self.active_readers -= 1

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._condition.notify_all()


class FakeStdin:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.closed = False

    def write(self, value: str) -> int:
        self.process.receive(json.loads(value))
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.stderr = None
        self.returncode: int | None = None
        self.methods: list[str] = []
        self.terminated = False

    def receive(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        self.methods.append(method)
        request_id = message.get("id")
        if method == "initialize":
            # A response for another id arriving first must be retained rather
            # than being mistaken for initialize's response.
            self.stdout.push({"jsonrpc": "2.0", "id": 99, "result": {"unrelated": True}})
            self.stdout.push({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "account/login/start":
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "type": "chatgptDeviceCode",
                        "loginId": "login-1",
                        "userCode": "ABCD-EFGH",
                        "verificationUrl": "https://example.test/device",
                    },
                }
            )
        elif method == "account/read":
            # This response is intentionally an already-authenticated old
            # account. It must never complete a device-code switch.
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "account": {"email": "old@example.com", "planType": "plus", "type": "chatgpt"},
                        "requiresOpenaiAuth": False,
                    },
                }
            )

    def push_completion(self, *, login_id: str | None, success: bool, error: str | None = None) -> None:
        self.stdout.push(
            {
                "jsonrpc": "2.0",
                "method": "account/login/completed",
                "params": {"success": success, "loginId": login_id, "error": error},
            }
        )

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.close()

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class FakePopen:
    def __init__(self) -> None:
        self.processes: list[FakeProcess] = []

    def __call__(self, _command: list[str], **_kwargs: Any) -> FakeProcess:
        process = FakeProcess()
        self.processes.append(process)
        return process


def _service(fake_popen: FakePopen, *, timeout: float = 0.2) -> CodexLoginService:
    client = CodexAppServerClient(
        codex_command=["codex"],
        timeout=0.2,
        popen_factory=fake_popen,
    )
    return CodexLoginService(
        client,
        login_timeout=timeout,
        poll_interval=0.02,
        lock=threading.Lock(),
    )


def test_device_code_keeps_process_alive_and_completes_by_matching_login_id() -> None:
    fake_popen = FakePopen()
    service = _service(fake_popen, timeout=1.0)
    done = threading.Event()
    results: list[DeviceLoginResult] = []

    start = service.start_device_login(on_complete=lambda result: (results.append(result), done.set()))
    process = fake_popen.processes[0]
    assert start.user_code == "ABCD-EFGH"
    assert process.terminated is False
    assert process.methods == ["initialize", "initialized", "account/login/start"]

    process.push_completion(login_id="other-login", success=True)
    time.sleep(0.05)
    assert done.is_set() is False
    assert process.terminated is False

    process.push_completion(login_id="login-1", success=True)
    assert done.wait(1.0)
    assert results[0].completed is True
    assert process.terminated is True
    assert process.stdout.max_active_readers == 1


def test_existing_account_without_completed_notification_times_out_and_is_cleaned() -> None:
    fake_popen = FakePopen()
    service = _service(fake_popen, timeout=0.12)
    done = threading.Event()
    results: list[DeviceLoginResult] = []

    service.start_device_login(on_complete=lambda result: (results.append(result), done.set()))
    process = fake_popen.processes[0]
    time.sleep(0.04)
    assert done.is_set() is False
    assert process.terminated is False
    assert "account/read" not in process.methods

    assert done.wait(1.0)
    assert results[0].completed is False
    assert results[0].error is not None
    assert process.terminated is True
    assert process.stdout.max_active_readers == 1


def test_login_timeout_cancellation_and_concurrency_cleanup() -> None:
    fake_popen = FakePopen()
    first = _service(fake_popen, timeout=1.0)
    second_client = CodexAppServerClient(codex_command=["codex"], popen_factory=fake_popen)
    second = CodexLoginService(second_client, login_timeout=1.0, lock=first._lock)
    done = threading.Event()

    first.start_device_login(on_complete=lambda _result: done.set())
    with pytest.raises(LoginInProgress):
        second.start_device_login()

    assert first.close(timeout=1.0) is True
    assert done.wait(1.0)
    assert fake_popen.processes[0].terminated is True
    assert first.cancel_device_login() is False
