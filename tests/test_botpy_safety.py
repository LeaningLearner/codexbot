from __future__ import annotations

from io import StringIO
import logging

from codexbot.botpy_safety import silence_botpy_logging


def test_botpy_logger_cannot_emit_access_tokens() -> None:
    stream = StringIO()
    logger = logging.getLogger("botpy")
    logger.handlers = [logging.StreamHandler(stream)]
    logger.propagate = True
    logger.setLevel(logging.INFO)

    silence_botpy_logging()
    logger.info({"access_token": "must-not-appear"})

    assert stream.getvalue() == ""
    assert logger.handlers == []
    assert logger.propagate is False
