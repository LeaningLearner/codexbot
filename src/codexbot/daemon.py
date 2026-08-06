from __future__ import annotations

import asyncio
import logging
import os

import psutil

from .locks import FileLock
from .logging_utils import configure_logging
from .paths import database_path, ensure_data_dir
from .processes import ensure_daemon, process_matches
from .qq_client import run_qq_runtime
from .security import Credentials, load_credentials, redact_secrets
from .store import Store


CLEANUP_INTERVAL_SECONDS = 60.0 * 60.0


def _lifecycle_work_remains(store: Store) -> bool:
    if store.companion_work_pending():
        return True
    return any(process_matches(host.pid, host.create_time) for host in store.list_hosts())


async def _periodic_cleanup(
    store: Store,
    logger: logging.Logger,
    stop_event: asyncio.Event,
    *,
    interval: float = CLEANUP_INTERVAL_SECONDS,
) -> None:
    """Run privacy cleanup periodically without doing SQLite work on the loop."""

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            continue
        except asyncio.TimeoutError:
            pass

        try:
            await asyncio.to_thread(store.cleanup)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = redact_secrets(str(exc))[:300]
            logger.error("Periodic store cleanup failed: %s: %s", type(exc).__name__, detail)


async def _wait_without_credentials(
    store: Store,
    logger: logging.Logger,
    *,
    poll_interval: float = 1.0,
) -> Credentials | None:
    """Wait for credentials while the Codex hosts that spawned us are alive."""

    empty_checks = 0
    while True:
        credentials = load_credentials()
        if credentials is not None:
            logger.info("QQ credentials became available; starting companion")
            return credentials

        hosts = store.list_hosts()
        dead = []
        for host in hosts:
            try:
                process = psutil.Process(host.pid)
                alive = process.is_running() and abs(process.create_time() - host.create_time) <= 0.25
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                alive = False
            if not alive:
                dead.append(host)
        if dead:
            store.remove_hosts(dead)
        if len(hosts) - len(dead):
            empty_checks = 0
        else:
            empty_checks += 1
            if empty_checks >= 2:
                logger.info("No Codex host remains; stopping companion")
                return None
        await asyncio.sleep(poll_interval)


async def _run_active_daemon(
    store: Store,
    logger: logging.Logger,
    *,
    cleanup_interval: float = CLEANUP_INTERVAL_SECONDS,
    standalone: bool = False,
) -> None:
    cleanup_stop = asyncio.Event()
    cleanup_task = asyncio.create_task(
        _periodic_cleanup(
            store,
            logger,
            cleanup_stop,
            interval=cleanup_interval,
        ),
        name="codexbot-store-cleanup",
    )
    try:
        credentials = load_credentials()
        if credentials is None:
            logger.error("QQ credentials are missing; run install.cmd or codexbot setup")
            credentials = await _wait_without_credentials(store, logger)
        if credentials is not None:
            await run_qq_runtime(store, credentials, logger, standalone=standalone)
    finally:
        cleanup_stop.set()
        await asyncio.gather(cleanup_task, return_exceptions=True)


def main() -> int:
    root = ensure_data_dir()
    singleton = FileLock(root / "daemon.lock", timeout=0.0)
    if not singleton.acquire():
        return 0

    logger = configure_logging("codexbot.daemon")
    store = Store(database_path())
    standalone = os.environ.get("CODEXBOT_STANDALONE") == "1"
    if standalone:
        logger.info("Running as a standalone companion (CODEXBOT_STANDALONE=1)")
    exit_code = 0
    try:
        try:
            created = psutil.Process(os.getpid()).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            created = 0.0
        store.set_daemon_info(os.getpid(), created)
        store.cleanup()
        asyncio.run(_run_active_daemon(store, logger, standalone=standalone))
    except Exception as exc:
        detail = redact_secrets(str(exc))[:300]
        logger.error("Companion stopped unexpectedly: %s: %s", type(exc).__name__, detail)
        exit_code = 1
    finally:
        # Clear the old PID before the final work check. A hook arriving before
        # this point is observed below; one arriving afterwards sees no live
        # daemon record and can start its own successor. Release the singleton
        # before spawning so the successor cannot lose the handoff race.
        store.clear_daemon_info(os.getpid())
        singleton.release()
        if _lifecycle_work_remains(store):
            ensure_daemon(store)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
