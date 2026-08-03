from __future__ import annotations

import logging


def silence_botpy_logging() -> None:
    """Disable BotPy logs because BotPy 1.1.5 logs access-token responses at INFO."""

    logger = logging.getLogger("botpy")
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)

