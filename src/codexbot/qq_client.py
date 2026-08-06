from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .botpy_safety import silence_botpy_logging
from .commands import CommandService
from .delivery import RateLimiter, deliver_item
from .processes import process_matches
from .security import Credentials, redact_secrets
from .store import Store


HOSTLESS_STARTUP_GRACE_SECONDS = 15.0


class QQRuntime:
    def __init__(
        self,
        store: Store,
        logger: logging.Logger,
        *,
        standalone: bool = False,
    ) -> None:
        self.store = store
        self.logger = logger
        self.standalone = standalone
        self.stop_event = asyncio.Event()
        self.ready_event = asyncio.Event()
        self.commands = CommandService(store)
        self.current_client: Any | None = None
        self.limiter = RateLimiter(per_minute=18)
        self.monitor_interval = 1.0
        self.empty_host_checks = 2
        # A detached Windows hook may outlive the short-lived Codex command
        # runner that invoked it.  Keep the companion alive long enough for
        # the QQ websocket to become ready and flush an already-queued final
        # notification even when no live host can be registered.
        self.hostless_startup_grace = HOSTLESS_STARTUP_GRACE_SECONDS
        # Leave enough time for rate-limited chunks of the final reply while
        # still guaranteeing that shutdown cannot wait forever.
        self.shutdown_drain_timeout = 60.0

    async def on_ready(self) -> None:
        self.logger.info("QQ sandbox client is ready")
        self.ready_event.set()

    async def on_c2c_message(self, client: Any, message: Any) -> None:
        openid = str(message.author.user_openid)
        message_id = str(message.id)
        content = str(message.content or "")

        async def passive_send(target: str, text: str, source_id: str, _sequence: int) -> object:
            return await message._api.post_c2c_message(
                openid=target,
                msg_type=0,
                msg_id=source_id,
                content=text,
            )

        async def active_send(target: str, text: str) -> object:
            active_client = self.current_client or client
            is_closed = getattr(active_client, "is_closed", None)
            if callable(is_closed) and is_closed():
                raise ConnectionError("QQ client is closed")
            return await active_client.api.post_c2c_message(
                openid=target,
                msg_type=0,
                content=text,
            )

        try:
            outcome = await self.commands.handle(
                openid=openid,
                message_id=message_id,
                content=content,
                passive_send=passive_send,
                active_send=active_send,
            )
            self.logger.info("Handled QQ command: %s", outcome)
        except Exception as exc:
            detail = redact_secrets(str(exc))[:300]
            self.logger.error("QQ command failed: %s: %s", type(exc).__name__, detail)

    async def monitor_hosts(self) -> None:
        started_at = time.monotonic()
        empty_checks = 0
        while not self.stop_event.is_set():
            hosts = self.store.list_hosts()
            dead = [host for host in hosts if not process_matches(host.pid, host.create_time)]
            if dead:
                self.store.remove_hosts(dead)
            alive_count = len(hosts) - len(dead)
            pending_work = self.store.companion_work_pending()
            if self.standalone:
                # Standalone mode keeps the QQ client online regardless of
                # Codex host activity; dead host rows are still pruned.
                await asyncio.sleep(self.monitor_interval)
                continue
            if alive_count:
                empty_checks = 0
            elif pending_work:
                # Do not let a websocket reconnect delay, QQ rate limit, or a
                # multi-part final reply strand reliable outbox work merely
                # because the short-lived Codex runner has already exited.
                empty_checks = 0
            else:
                within_startup_grace = (
                    not self.ready_event.is_set()
                    and time.monotonic() - started_at < self.hostless_startup_grace
                )
                if within_startup_grace:
                    empty_checks = 0
                    await asyncio.sleep(self.monitor_interval)
                    continue
                empty_checks += 1
                if empty_checks >= self.empty_host_checks:
                    self.logger.info("No Codex host remains; stopping companion")
                    self.stop_event.set()
                    return
            await asyncio.sleep(self.monitor_interval)

    async def drain_outbox(self, client: Any) -> None:
        """Deliver due notifications before closing the last QQ connection."""

        if not self.ready_event.is_set() or client.is_closed():
            return

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_drain_timeout
        retry_wait = False

        async def active_send(openid: str, text: str) -> object:
            return await client.api.post_c2c_message(openid=openid, msg_type=0, content=text)

        while not client.is_closed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.logger.warning("Shutdown outbox drain deadline reached")
                return

            openid = self.store.get_bound_openid()
            item = self.store.get_due_outbox()
            if not openid or item is None:
                if retry_wait:
                    await asyncio.sleep(min(1.0, remaining))
                    continue
                return

            try:
                outcome = await asyncio.wait_for(
                    deliver_item(
                        self.store,
                        item,
                        openid,
                        active_send,
                        self.limiter,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                self.logger.warning("Shutdown outbox drain timed out")
                return
            except Exception as exc:
                detail = redact_secrets(str(exc))[:300]
                self.logger.error(
                    "Shutdown outbox drain failed: %s: %s",
                    type(exc).__name__,
                    detail,
                )
                return
            retry_wait = outcome == "retry"

    async def delivery_loop(self, client: Any) -> None:
        await self.ready_event.wait()

        async def active_send(openid: str, text: str) -> object:
            return await client.api.post_c2c_message(openid=openid, msg_type=0, content=text)

        while not self.stop_event.is_set() and not client.is_closed():
            openid = self.store.get_bound_openid()
            if not openid:
                await asyncio.sleep(1.0)
                continue
            item = self.store.get_due_outbox()
            if item is None:
                await asyncio.sleep(0.5)
                continue
            outcome = await deliver_item(self.store, item, openid, active_send, self.limiter)
            if outcome == "retry":
                await asyncio.sleep(1.0)
            elif outcome == "failed_permanent":
                self.logger.warning("QQ rejected proactive message permanently; use /last for the final reply")


def _make_client(runtime: QQRuntime) -> Any:
    silence_botpy_logging()
    import botpy

    class CodexBotClient(botpy.Client):
        async def on_ready(self) -> None:
            await runtime.on_ready()

        async def on_c2c_message_create(self, message: Any) -> None:
            await runtime.on_c2c_message(self, message)

    intents = botpy.Intents(public_messages=True)
    return CodexBotClient(
        intents=intents,
        is_sandbox=True,
        timeout=8,
        bot_log=False,
        ext_handlers=False,
        log_level=logging.WARNING,
    )


def _report_task_exit(
    logger: logging.Logger,
    task: asyncio.Task[Any],
    label: str,
    *,
    expected: bool,
) -> bool:
    """Log a task that ended and return whether it ended unexpectedly."""

    if task.cancelled():
        if not expected:
            logger.error("%s was cancelled unexpectedly", label)
            return True
        return False

    error = task.exception()
    if error is not None:
        detail = redact_secrets(str(error))[:300]
        logger.error("%s failed: %s: %s", label, type(error).__name__, detail)
        return True
    if not expected:
        logger.warning("%s exited unexpectedly", label)
        return True
    return False


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _close_client(client: Any, logger: logging.Logger) -> None:
    try:
        await client.close()
    except Exception as exc:
        detail = redact_secrets(str(exc))[:300]
        logger.error("QQ client close failed: %s: %s", type(exc).__name__, detail)


async def _shutdown_client(
    runtime: QQRuntime,
    client: Any,
    bot_task: asyncio.Task[Any],
    delivery_task: asyncio.Task[Any],
    stop_task: asyncio.Task[Any],
    logger: logging.Logger,
    *,
    drain: bool,
) -> None:
    await _cancel_task(delivery_task)
    try:
        if drain:
            try:
                await runtime.drain_outbox(client)
            except Exception as exc:
                detail = redact_secrets(str(exc))[:300]
                logger.error("QQ shutdown drain failed: %s: %s", type(exc).__name__, detail)
    finally:
        await _close_client(client, logger)
        if runtime.current_client is client:
            runtime.current_client = None
        await _cancel_task(bot_task)
        await _cancel_task(stop_task)


async def run_qq_runtime(
    store: Store,
    credentials: Credentials,
    logger: logging.Logger,
    *,
    initial_reconnect_delay: float = 3.0,
    standalone: bool = False,
) -> None:
    runtime = QQRuntime(store, logger, standalone=standalone)
    monitor_task = asyncio.create_task(runtime.monitor_hosts(), name="codexbot-host-monitor")
    reconnect_delay = initial_reconnect_delay
    active_client: tuple[Any, asyncio.Task[Any], asyncio.Task[Any], asyncio.Task[Any]] | None = None
    try:
        while not runtime.stop_event.is_set():
            if monitor_task.done():
                unexpected = _report_task_exit(
                    logger,
                    monitor_task,
                    "Codex host monitor",
                    expected=runtime.stop_event.is_set(),
                )
                if unexpected:
                    runtime.stop_event.set()
                break

            client = _make_client(runtime)
            runtime.current_client = client
            runtime.ready_event.clear()
            bot_task = asyncio.create_task(
                client.start(appid=credentials.app_id, secret=credentials.app_secret),
                name="codexbot-qq-client",
            )
            delivery_task = asyncio.create_task(runtime.delivery_loop(client), name="codexbot-delivery")
            stop_task = asyncio.create_task(runtime.stop_event.wait(), name="codexbot-stop-wait")
            active_client = (client, bot_task, delivery_task, stop_task)
            done, _ = await asyncio.wait(
                {bot_task, delivery_task, monitor_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            stop_requested = stop_task in done or runtime.stop_event.is_set()
            if monitor_task in done:
                unexpected = _report_task_exit(
                    logger,
                    monitor_task,
                    "Codex host monitor",
                    expected=stop_requested,
                )
                if unexpected:
                    runtime.stop_event.set()
                    stop_requested = True

            if bot_task in done:
                _report_task_exit(
                    logger,
                    bot_task,
                    "QQ connection task",
                    expected=stop_requested,
                )
            if delivery_task in done:
                unexpected = _report_task_exit(
                    logger,
                    delivery_task,
                    "QQ delivery loop",
                    expected=stop_requested,
                )
                if unexpected and not stop_requested:
                    logger.warning("QQ delivery loop stopped; reconnecting")

            if stop_requested or runtime.stop_event.is_set():
                await _shutdown_client(
                    runtime,
                    client,
                    bot_task,
                    delivery_task,
                    stop_task,
                    logger,
                    drain=True,
                )
                active_client = None
                break

            await _shutdown_client(
                runtime,
                client,
                bot_task,
                delivery_task,
                stop_task,
                logger,
                drain=False,
            )
            active_client = None

            if runtime.ready_event.is_set():
                reconnect_delay = initial_reconnect_delay
            try:
                await asyncio.wait_for(runtime.stop_event.wait(), timeout=reconnect_delay)
            except asyncio.TimeoutError:
                reconnect_delay = min(60.0, reconnect_delay * 2)
    except asyncio.CancelledError:
        runtime.stop_event.set()
        if active_client is not None:
            client, bot_task, delivery_task, stop_task = active_client
            await _shutdown_client(
                runtime,
                client,
                bot_task,
                delivery_task,
                stop_task,
                logger,
                drain=True,
            )
            active_client = None
        raise
    finally:
        runtime.stop_event.set()
        if active_client is not None:
            client, bot_task, delivery_task, stop_task = active_client
            await _shutdown_client(
                runtime,
                client,
                bot_task,
                delivery_task,
                stop_task,
                logger,
                drain=True,
            )
        await runtime.commands.shutdown()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
