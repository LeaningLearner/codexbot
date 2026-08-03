from __future__ import annotations

import asyncio
import logging
from typing import Any

from .botpy_safety import silence_botpy_logging
from .commands import CommandService
from .delivery import RateLimiter, deliver_item
from .processes import process_matches
from .security import Credentials, redact_secrets
from .store import Store


class QQRuntime:
    def __init__(self, store: Store, logger: logging.Logger) -> None:
        self.store = store
        self.logger = logger
        self.stop_event = asyncio.Event()
        self.ready_event = asyncio.Event()
        self.commands = CommandService(store)
        self.limiter = RateLimiter(per_minute=18)
        self.monitor_interval = 1.0
        self.empty_host_checks = 2

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
            return await client.api.post_c2c_message(openid=target, msg_type=0, content=text)

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
        empty_checks = 0
        while not self.stop_event.is_set():
            hosts = self.store.list_hosts()
            dead = [host for host in hosts if not process_matches(host.pid, host.create_time)]
            if dead:
                self.store.remove_hosts(dead)
            alive_count = len(hosts) - len(dead)
            if alive_count:
                empty_checks = 0
            else:
                empty_checks += 1
                if empty_checks >= self.empty_host_checks:
                    self.logger.info("No Codex host remains; stopping companion")
                    self.stop_event.set()
                    return
            await asyncio.sleep(self.monitor_interval)

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


async def run_qq_runtime(
    store: Store,
    credentials: Credentials,
    logger: logging.Logger,
    *,
    initial_reconnect_delay: float = 3.0,
) -> None:
    runtime = QQRuntime(store, logger)
    monitor_task = asyncio.create_task(runtime.monitor_hosts(), name="codexbot-host-monitor")
    reconnect_delay = initial_reconnect_delay
    try:
        while not runtime.stop_event.is_set():
            client = _make_client(runtime)
            runtime.ready_event.clear()
            bot_task = asyncio.create_task(
                client.start(appid=credentials.app_id, secret=credentials.app_secret),
                name="codexbot-qq-client",
            )
            delivery_task = asyncio.create_task(runtime.delivery_loop(client), name="codexbot-delivery")
            stop_task = asyncio.create_task(runtime.stop_event.wait(), name="codexbot-stop-wait")
            done, _ = await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done:
                await client.close()
                bot_task.cancel()
                delivery_task.cancel()
                await asyncio.gather(bot_task, delivery_task, return_exceptions=True)
                break

            stop_task.cancel()
            delivery_task.cancel()
            await asyncio.gather(stop_task, delivery_task, return_exceptions=True)
            error = bot_task.exception()
            if error:
                detail = redact_secrets(str(error))[:300]
                logger.error("QQ connection ended: %s: %s", type(error).__name__, detail)
            await client.close()
            if runtime.ready_event.is_set():
                reconnect_delay = initial_reconnect_delay
            try:
                await asyncio.wait_for(runtime.stop_event.wait(), timeout=reconnect_delay)
            except asyncio.TimeoutError:
                reconnect_delay = min(60.0, reconnect_delay * 2)
    finally:
        runtime.stop_event.set()
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
