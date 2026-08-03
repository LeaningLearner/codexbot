from __future__ import annotations

import asyncio
import logging
import os

import psutil

from .locks import FileLock
from .logging_utils import configure_logging
from .paths import database_path, ensure_data_dir
from .qq_client import run_qq_runtime
from .security import load_credentials, redact_secrets
from .store import Store


async def _wait_without_credentials(store: Store, logger: logging.Logger) -> None:
    empty_checks = 0
    while True:
        hosts = store.list_hosts()
        dead = []
        for host in hosts:
            try:
                process = psutil.Process(host.pid)
                alive = process.is_running() and abs(process.create_time() - host.create_time) <= 0.25
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
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
                return
        await asyncio.sleep(1.0)


def main() -> int:
    root = ensure_data_dir()
    singleton = FileLock(root / "daemon.lock", timeout=0.0)
    if not singleton.acquire():
        return 0

    logger = configure_logging("codexbot.daemon")
    store = Store(database_path())
    try:
        try:
            created = psutil.Process(os.getpid()).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            created = 0.0
        store.set_daemon_info(os.getpid(), created)
        store.cleanup()
        credentials = load_credentials()
        if credentials is None:
            logger.error("QQ credentials are missing; run install.cmd or codexbot setup")
            asyncio.run(_wait_without_credentials(store, logger))
        else:
            asyncio.run(run_qq_runtime(store, credentials, logger))
    except Exception as exc:
        logger.exception("Companion stopped unexpectedly: %s", redact_secrets(str(exc))[:300])
        return 1
    finally:
        store.clear_daemon_info(os.getpid())
        singleton.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
