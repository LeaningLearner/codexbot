from __future__ import annotations

import logging
import multiprocessing
import os
from pathlib import Path

from codexbot.logging_utils import ProcessSafeRotatingFileHandler


def _write_from_worker(log_path: Path) -> None:
    handler = ProcessSafeRotatingFileHandler(log_path, maxBytes=128, backupCount=2)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"test-process-safe-worker-{os.getpid()}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for _ in range(30):
            logger.info("worker %s", os.getpid())
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_process_safe_handler_keeps_log_and_backups_bounded(tmp_path: Path) -> None:
    log_path = tmp_path / "codexbot.log"
    handler = ProcessSafeRotatingFileHandler(log_path, maxBytes=128, backupCount=2)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test-process-safe-logging")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for _ in range(30):
            logger.info("x" * 80)
    finally:
        logger.removeHandler(handler)
        handler.close()

    log_files = [log_path, log_path.with_name("codexbot.log.1"), log_path.with_name("codexbot.log.2")]
    assert any(path.is_file() for path in log_files)
    assert all(not path.exists() or path.stat().st_size <= 128 for path in log_files)
    assert not log_path.with_name("codexbot.log.3").exists()


def test_process_safe_handler_serializes_multiple_processes(tmp_path: Path) -> None:
    log_path = tmp_path / "codexbot.log"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_write_from_worker, args=(log_path,)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(15)

    assert all(worker.exitcode == 0 for worker in workers)
    log_files = [log_path, log_path.with_name("codexbot.log.1"), log_path.with_name("codexbot.log.2")]
    assert any(path.is_file() for path in log_files)
    assert all(not path.exists() or path.stat().st_size <= 128 for path in log_files)
