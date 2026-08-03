from __future__ import annotations

import logging
from pathlib import Path
import threading

from .locks import FileLock
from .paths import logs_dir


LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3

_thread_locks: dict[Path, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: Path) -> threading.RLock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(path, threading.RLock())


class ProcessSafeRotatingFileHandler(logging.Handler):
    """A bounded file handler safe for hooks and daemon processes sharing a log.

    The log file is opened only while an inter-process lock is held. This keeps
    rotation from racing between short-lived hook processes and the daemon,
    unlike a normal ``RotatingFileHandler`` that keeps an independent stream in
    every process.
    """

    def __init__(
        self,
        filename: str | Path,
        *,
        maxBytes: int = LOG_MAX_BYTES,
        backupCount: int = LOG_BACKUP_COUNT,
        encoding: str = "utf-8",
        lock_timeout: float = 1.0,
    ) -> None:
        super().__init__()
        self.baseFilename = str(Path(filename).resolve())
        self.maxBytes = max(1, int(maxBytes))
        self.backupCount = max(0, int(backupCount))
        self.encoding = encoding
        self.lock_timeout = max(0.0, lock_timeout)
        self._path = Path(self.baseFilename)
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    def _rotate(self) -> None:
        if self.backupCount == 0:
            self._path.unlink(missing_ok=True)
            return

        for index in range(self.backupCount, 0, -1):
            target = self._path.with_name(f"{self._path.name}.{index}")
            source = self._path if index == 1 else self._path.with_name(
                f"{self._path.name}.{index - 1}"
            )
            if target.exists():
                target.unlink()
            if source.exists():
                source.replace(target)

    def _record_bytes(self, record: logging.LogRecord) -> bytes:
        rendered = self.format(record) + "\n"
        data = rendered.encode(self.encoding, errors="replace")
        if len(data) <= self.maxBytes:
            return data

        marker = "... [log record truncated]\n".encode(self.encoding)
        if len(marker) >= self.maxBytes:
            return marker[: self.maxBytes]
        prefix_limit = self.maxBytes - len(marker)
        prefix = data[:prefix_limit].decode(self.encoding, errors="ignore").encode(self.encoding)
        return (prefix + marker)[: self.maxBytes]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            data = self._record_bytes(record)
            thread_lock = _thread_lock(self._path)
            with thread_lock:
                process_lock = FileLock(self._lock_path, timeout=self.lock_timeout)
                if not process_lock.acquire():
                    return
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    current_size = self._path.stat().st_size if self._path.exists() else 0
                    if current_size and current_size + len(data) > self.maxBytes:
                        self._rotate()
                    with self._path.open("ab") as handle:
                        handle.write(data)
                        handle.flush()
                finally:
                    process_lock.release()
        except Exception:
            self.handleError(record)


def configure_logging(name: str = "codexbot", *, verbose: bool = False) -> logging.Logger:
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = ProcessSafeRotatingFileHandler(
            directory / "codexbot.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger
