from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import logs_dir


def configure_logging(name: str = "codexbot", *, verbose: bool = False) -> logging.Logger:
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            directory / "codexbot.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger

