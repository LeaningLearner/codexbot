from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from queue import Empty, Queue
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from .installer import find_codex_command
from .security import redact_secrets


APP_SERVER_DASHBOARD_URL = "https://chatgpt.com/codex/settings/usage"
DEFAULT_APP_SERVER_TIMEOUT = 20.0
DEFAULT_DEVICE_LOGIN_TIMEOUT = 10 * 60.0
DEFAULT_DEVICE_LOGIN_POLL_INTERVAL = 1.0
APP_SERVER_CLIENT_INFO = {
    "name": "codexbot",
    "title": "CodexBot",
    "version": "0.1.0",
}


def _safe_detail(value: object, *, limit: int = 240) -> str:
    """Keep app-server failures useful without copying credentials to callers."""

    text = redact_secrets(str(value or "")).replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:limit]


class AppServerError(RuntimeError):
    """Base class for a local Codex app-server failure."""


class AppServerUnavailable(AppServerError):
    pass


class AppServerTimeout(AppServerError):
    pass


class AppServerProtocolError(AppServerError):
    pass


class AppServerRPCError(AppServerError):
    def __init__(self, code: int | None, message: object, data: object = None) -> None:
        self.code = code
        self.message = _safe_detail(message)
        # Do not retain arbitrary error data. It can contain a token or a path
        # copied from a Codex authentication response.
        self.data_summary = _safe_detail(data) if data is not None else ""
        detail = self.message or "Codex app-server returned an error"
        super().__init__(f"RPC error {code}: {detail}" if code is not None else detail)


class LoginCancelled(AppServerError):
    pass


class LoginFailed(AppServerError):
    pass


@dataclass(frozen=True)
class AccountInfo:
    email: str | None
    plan: str | None
    auth_type: str
    requires_openai_auth: bool = False

    @property
    def is_authenticated(self) -> bool:
        return bool(self.email or self.plan or self.auth_type not in {"", "unknown", "not_logged_in"}) and not (
            self.requires_openai_auth and self.auth_type == "not_logged_in"
        )


@dataclass(frozen=True)
class DeviceLoginStart:
    login_id: str
    verification_url: str
    user_code: str


@dataclass(frozen=True)
class DeviceLoginResult:
    started: DeviceLoginStart
    completed: bool
    account: AccountInfo | None = None
    error: str | None = None
    cancelled: bool = False


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_account_result(payload: object) -> AccountInfo:
    """Extract only display-safe account fields from ``account/read``."""

    if isinstance(payload, AccountInfo):
        return payload
    root = _as_mapping(payload)
    account_value = root.get("account") if "account" in root else payload
    account = _as_mapping(account_value)
    requires_auth = bool(root.get("requiresOpenaiAuth"))
    if not account:
        return AccountInfo(
            email=None,
            plan=None,
            auth_type="not_logged_in" if requires_auth else "unknown",
            requires_openai_auth=requires_auth,
        )

    raw_auth_type = _first_text(
        account.get("authType"),
        account.get("auth_type"),
        account.get("type"),
        root.get("authType"),
    )
    return AccountInfo(
        email=_first_text(account.get("email"), account.get("emailAddress")),
        plan=_first_text(
            account.get("planType"),
            account.get("plan"),
            account.get("subscriptionType"),
        ),
        auth_type=raw_auth_type or "unknown",
        requires_openai_auth=requires_auth,
    )


def parse_device_login_result(payload: object) -> DeviceLoginStart:
    if isinstance(payload, DeviceLoginStart):
        return payload
    result = _as_mapping(payload)
    login_type = _first_text(result.get("type"))
    login_id = _first_text(result.get("loginId"), result.get("login_id"))
    verification_url = _first_text(result.get("verificationUrl"), result.get("verification_url"))
    user_code = _first_text(result.get("userCode"), result.get("user_code"))
    if not login_type or not login_id or not verification_url or not user_code:
        raise AppServerProtocolError("account/login/start did not return a complete device code")
    return DeviceLoginStart(
        login_id=login_id,
        verification_url=verification_url,
        user_code=user_code,
    )


PopenFactory = Callable[..., Any]


class CodexAppServerClient:
    """JSONL JSON-RPC client for the stable Codex app-server auth endpoints.

    Normal account/usage calls are one-shot. Device-code login uses
    ``start_device_login_session`` so the same child remains alive while the
    user completes the code flow. Every response is matched by its JSON-RPC
    id; notifications and out-of-order responses are never treated as the
    next response by position.
    """

    def __init__(
        self,
        codex_command: str | os.PathLike[str] | Sequence[str] | None = None,
        *,
        timeout: float = DEFAULT_APP_SERVER_TIMEOUT,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        self.codex_command = codex_command
        self.timeout = max(float(timeout), 0.1)
        self._popen_factory = popen_factory or subprocess.Popen
        self._rpc_lock = threading.Lock()
        self._process: Any = None
        self._pending: dict[object, dict[str, Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._next_id = 0
        self._reader_queue: Queue[tuple[str | bytes | None, BaseException | None]] | None = None
        self._reader_thread: threading.Thread | None = None

    def _command(self) -> list[str]:
        command = self.codex_command
        if command is None:
            command = find_codex_command()
        if not command:
            raise AppServerUnavailable("找不到 codex/codex.cmd")
        if isinstance(command, (str, os.PathLike)):
            return [os.fspath(command), "app-server"]
        return [str(part) for part in command] + ["app-server"]

    @contextmanager
    def _running_process(self) -> Iterator[Any]:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        try:
            process = self._popen_factory(self._command(), **kwargs)
        except (OSError, ValueError) as exc:
            raise AppServerUnavailable(f"无法启动 Codex app-server：{type(exc).__name__}") from None

        self._process = process
        try:
            if getattr(process, "stdin", None) is None or getattr(process, "stdout", None) is None:
                raise AppServerUnavailable("Codex app-server 未提供 stdio")
            self._start_reader(process)
            yield process
        finally:
            self._close_process(process)
            self._process = None

    def _start_reader(self, process: Any) -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            raise AppServerUnavailable("Codex app-server stdout 已关闭")
        result_queue: Queue[tuple[str | bytes | None, BaseException | None]] = Queue()

        def read_loop() -> None:
            while True:
                try:
                    line = stream.readline()
                except BaseException as exc:  # pragma: no cover - depends on a broken OS pipe
                    result_queue.put((None, exc))
                    return
                if line is None or line == "" or line == b"":
                    result_queue.put((None, None))
                    return
                result_queue.put((line, None))

        self._reader_queue = result_queue
        self._reader_thread = threading.Thread(
            target=read_loop,
            name="codexbot-app-server-reader",
            daemon=True,
        )
        self._reader_thread.start()

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    def _close_process(self, process: Any) -> None:
        self._close_stream(getattr(process, "stdin", None))
        poll = getattr(process, "poll", None)
        try:
            alive = poll is None or poll() is None
        except Exception:
            alive = True
        if alive:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass
        self._close_stream(getattr(process, "stdout", None))
        self._close_stream(getattr(process, "stderr", None))
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader_thread = None
        self._reader_queue = None

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerTimeout("Codex app-server 请求超时")
        return remaining

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        stream = getattr(process, "stdin", None)
        if stream is None:
            raise AppServerUnavailable("Codex app-server stdin 已关闭")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            stream.write(encoded)
            stream.flush()
        except (OSError, ValueError, BrokenPipeError) as exc:
            raise AppServerUnavailable(f"Codex app-server stdin 失败：{type(exc).__name__}") from None

    def _readline(self, deadline: float) -> str:
        result_queue = self._reader_queue
        if result_queue is None:
            raise AppServerUnavailable("Codex app-server reader 已关闭")
        try:
            line, error = result_queue.get(timeout=self._remaining(deadline))
        except Empty:
            raise AppServerTimeout("Codex app-server 响应超时") from None
        if error is not None:
            raise AppServerUnavailable(f"Codex app-server stdout 失败：{type(error).__name__}") from None
        if line is None or line == "" or line == b"":
            raise AppServerUnavailable("Codex app-server 提前退出")
        if isinstance(line, bytes):
            return line.decode("utf-8", errors="replace")
        return str(line)

    def _read_message(self, deadline: float) -> dict[str, Any]:
        while True:
            line = self._readline(deadline)
            try:
                message = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                # Some old wrappers print a harmless non-JSON line. Ignore it
                # and keep waiting for a JSON-RPC message until the deadline.
                continue
            if isinstance(message, dict):
                return message

    def _response_for(self, request_id: object, deadline: float) -> object:
        pending = self._pending.pop(request_id, None)
        while pending is None:
            message = self._read_message(deadline)
            if "id" not in message:
                if "method" in message:
                    self._notifications.append(message)
                continue
            response_id = message.get("id")
            # A server-to-client request is not a response to our request.
            if "method" in message and "result" not in message and "error" not in message:
                continue
            if response_id != request_id:
                try:
                    self._pending[response_id] = message
                except TypeError:
                    continue
                continue
            pending = message

        if "error" in pending:
            error = _as_mapping(pending.get("error"))
            code = error.get("code")
            try:
                numeric_code = int(code) if code is not None else None
            except (TypeError, ValueError):
                numeric_code = None
            raise AppServerRPCError(numeric_code, error.get("message"), error.get("data"))
        if "result" not in pending:
            raise AppServerProtocolError("Codex app-server response missing result")
        return pending["result"]

    def _request(self, method: str, params: Mapping[str, Any], deadline: float) -> object:
        request_id = self._next_id
        self._next_id += 1
        if request_id not in self._pending:
            self._send({"method": method, "id": request_id, "params": dict(params)})
        return self._response_for(request_id, deadline)

    def _initialize(self, deadline: float) -> None:
        self._request(
            "initialize",
            {"clientInfo": dict(APP_SERVER_CLIENT_INFO)},
            deadline,
        )
        self._send({"method": "initialized", "params": {}})

    def open_session(self, *, timeout: float | None = None) -> CodexAppServerSession:
        session = CodexAppServerSession(self, timeout=timeout or self.timeout)
        return session.start()

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> object:
        session = self.open_session(timeout=timeout)
        try:
            return session.request(method, params or {}, timeout=timeout)
        finally:
            session.close()

    def read_account(self) -> AccountInfo:
        return parse_account_result(self.call("account/read", {"refreshToken": False}))

    def account_read(self) -> AccountInfo:
        return self.read_account()

    def read_rate_limits(self) -> Mapping[str, Any]:
        payload = self.call("account/rateLimits/read", {})
        if not isinstance(payload, Mapping):
            raise AppServerProtocolError("account/rateLimits/read returned an invalid result")
        return payload

    def rate_limits_read(self) -> Mapping[str, Any]:
        return self.read_rate_limits()

    def start_device_login_session(self) -> tuple[CodexAppServerSession, DeviceLoginStart]:
        session = self.open_session()
        try:
            start = parse_device_login_result(
                session.request("account/login/start", {"type": "chatgptDeviceCode"})
            )
        except Exception:
            session.close()
            raise
        return session, start

    def start_device_login(
        self,
        on_complete: LoginCompletionCallback | None = None,
        *,
        login_timeout: float = DEFAULT_DEVICE_LOGIN_TIMEOUT,
    ) -> DeviceLoginStart:
        """Start a managed device login without closing its app-server child."""

        service = CodexLoginService(self, login_timeout=login_timeout)
        return service.start_device_login(on_complete=on_complete)

    def login_start(
        self,
        on_complete: LoginCompletionCallback | None = None,
        *,
        login_timeout: float = DEFAULT_DEVICE_LOGIN_TIMEOUT,
    ) -> DeviceLoginStart:
        return self.start_device_login(on_complete=on_complete, login_timeout=login_timeout)


class CodexAppServerSession:
    """An initialized app-server process that can outlive the QQ command."""

    def __init__(self, client: CodexAppServerClient, *, timeout: float) -> None:
        self.client = client
        self.timeout = max(float(timeout), 0.1)
        self._manager: Any = None
        self._closed = False
        self._lock_acquired = False

    def start(self) -> CodexAppServerSession:
        if self._manager is not None:
            return self
        if not self.client._rpc_lock.acquire(timeout=self.timeout):
            raise AppServerTimeout("Codex app-server 正在处理另一个请求")
        self._lock_acquired = True
        self.client._pending = {}
        self.client._notifications = []
        self.client._next_id = 0
        self._manager = self.client._running_process()
        try:
            self._manager.__enter__()
            self.client._initialize(time.monotonic() + self.timeout)
        except Exception:
            self.close()
            raise
        return self

    def __enter__(self) -> CodexAppServerSession:
        return self.start()

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> object:
        if self._manager is None or self._closed:
            raise AppServerUnavailable("Codex app-server session is closed")
        request_timeout = self.timeout if timeout is None else max(float(timeout), 0.1)
        return self.client._request(method, params or {}, time.monotonic() + request_timeout)

    def _notification_login_result(
        self,
        message: Mapping[str, Any],
        login_id: str,
    ) -> AccountInfo | None | object:
        method = str(message.get("method") or "").casefold()
        if method != "account/login/completed":
            return _NO_LOGIN_RESULT
        params = _as_mapping(message.get("params"))
        notification_login_id = params.get("loginId")
        if notification_login_id is not None and str(notification_login_id) != login_id:
            return _NO_LOGIN_RESULT
        success = params.get("success")
        if success is not True and success is not False:
            raise AppServerProtocolError("account/login/completed missing boolean success")
        if not success:
            raise LoginFailed(_safe_detail(params.get("error")) or "设备码登录失败")
        account = params.get("account")
        return parse_account_result(account) if account is not None else None

    def _take_login_notification(self, login_id: str) -> AccountInfo | None | object:
        remaining: list[dict[str, Any]] = []
        result: AccountInfo | None | object = _NO_LOGIN_RESULT
        for message in self.client._notifications:
            candidate = self._notification_login_result(message, login_id)
            if candidate is _NO_LOGIN_RESULT:
                remaining.append(message)
            else:
                result = candidate
                break
        self.client._notifications = remaining
        return result

    def wait_for_login(
        self,
        login_id: str,
        *,
        timeout: float,
        poll_interval: float = DEFAULT_DEVICE_LOGIN_POLL_INTERVAL,
        cancel_event: threading.Event | None = None,
    ) -> AccountInfo | None:
        deadline = time.monotonic() + max(float(timeout), 0.1)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise LoginCancelled("设备码登录已取消")
            notification = self._take_login_notification(login_id)
            if notification is not _NO_LOGIN_RESULT:
                return notification if isinstance(notification, AccountInfo) else None

            # An existing account/read result is not proof that this device
            # login completed: account switching commonly starts with an old
            # authenticated account. The stable completion notification and a
            # matching loginId are therefore the only success signal.
            try:
                message = self.client._read_message(deadline)
            except AppServerTimeout:
                raise AppServerTimeout("设备码登录超时") from None
            if "id" in message:
                response_id = message.get("id")
                if "method" not in message or "result" in message or "error" in message:
                    try:
                        self.client._pending[response_id] = message
                    except TypeError:
                        pass
                continue
            if "method" in message:
                self.client._notifications.append(message)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        manager = self._manager
        self._manager = None
        try:
            if manager is not None:
                manager.__exit__(None, None, None)
        finally:
            if self._lock_acquired:
                self._lock_acquired = False
                self.client._rpc_lock.release()


_NO_LOGIN_RESULT = object()
_ACCOUNT_LOGIN_LOCK = threading.Lock()


class LoginInProgress(AppServerError):
    pass


LoginCompletionCallback = Callable[[DeviceLoginResult], None]


class CodexLoginService:
    """Keep one device-code login app-server alive until it completes."""

    def __init__(
        self,
        client: CodexAppServerClient | Any | None = None,
        *,
        login_timeout: float = DEFAULT_DEVICE_LOGIN_TIMEOUT,
        poll_interval: float = DEFAULT_DEVICE_LOGIN_POLL_INTERVAL,
        lock: threading.Lock | None = None,
    ) -> None:
        self.client = client or CodexAppServerClient()
        self.login_timeout = max(float(login_timeout), 0.1)
        self.poll_interval = max(float(poll_interval), 0.01)
        self._lock = lock or _ACCOUNT_LOGIN_LOCK
        self._state_lock = threading.Lock()
        self._active_session: CodexAppServerSession | None = None
        self._cancel_event: threading.Event | None = None
        self._worker: threading.Thread | None = None

    def start_device_login(self, on_complete: LoginCompletionCallback | None = None) -> DeviceLoginStart:
        if not self._lock.acquire(blocking=False):
            raise LoginInProgress("已有 Codex 账号切换正在进行")
        session: CodexAppServerSession | None = None
        try:
            if hasattr(self.client, "start_device_login_session"):
                session, start = self.client.start_device_login_session()
            else:  # Test doubles from older callers can still provide the start response.
                start = parse_device_login_result(self.client.start_device_login())
                session = None
            cancel_event = threading.Event()
            with self._state_lock:
                self._active_session = session
                self._cancel_event = cancel_event
            worker = threading.Thread(
                target=self._complete_login,
                args=(start, session, cancel_event, on_complete),
                name="codexbot-device-login",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return start
        except Exception:
            if session is not None:
                session.close()
            self._clear_active(session)
            self._lock.release()
            raise

    def _clear_active(self, session: CodexAppServerSession | None) -> None:
        with self._state_lock:
            if session is None or self._active_session is session:
                self._active_session = None
                self._cancel_event = None

    def _complete_login(
        self,
        start: DeviceLoginStart,
        session: CodexAppServerSession | None,
        cancel_event: threading.Event,
        on_complete: LoginCompletionCallback | None,
    ) -> None:
        completed = False
        account: AccountInfo | None = None
        error: str | None = None
        cancelled = False
        try:
            if session is None:
                raise AppServerProtocolError("device login session is unavailable")
            account = session.wait_for_login(
                start.login_id,
                timeout=self.login_timeout,
                poll_interval=self.poll_interval,
                cancel_event=cancel_event,
            )
            completed = True
        except LoginCancelled:
            cancelled = True
            error = "设备码登录已取消"
        except Exception as exc:
            if cancel_event.is_set():
                cancelled = True
                error = "设备码登录已取消"
            else:
                error = _safe_detail(exc) or type(exc).__name__
        finally:
            if session is not None:
                session.close()
            self._clear_active(session)
            self._lock.release()
            result = DeviceLoginResult(
                started=start,
                completed=completed,
                account=account,
                error=error,
                cancelled=cancelled,
            )
            if on_complete is not None:
                try:
                    on_complete(result)
                except Exception:
                    # Completion delivery must never keep the app-server child
                    # or the account lock alive.
                    pass

    def cancel_device_login(self) -> bool:
        with self._state_lock:
            session = self._active_session
            cancel_event = self._cancel_event
        if session is None or cancel_event is None:
            return False
        cancel_event.set()
        session.close()
        return True

    def close(self, *, timeout: float = 3.0) -> bool:
        """Cancel an active login and wait for its worker to release state."""

        cancelled = self.cancel_device_login()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(float(timeout), 0.0))
        return cancelled

    def login_device_code(
        self,
        on_complete: LoginCompletionCallback | None = None,
    ) -> DeviceLoginStart:
        return self.start_device_login(on_complete=on_complete)
