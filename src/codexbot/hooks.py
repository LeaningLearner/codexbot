from __future__ import annotations

import json
import os
import sys

from .logging_utils import configure_logging
from .paths import database_path, ensure_data_dir
from .processes import discover_codex_host, ensure_daemon
from .security import redact_secrets
from .store import Store


def process_hook(payload: dict[str, object], store: Store) -> bool:
    host = discover_codex_host()
    inserted = store.ingest_hook(payload, host)
    if (
        payload.get("hook_event_name") != "SessionEnd"
        and os.environ.get("CODEXBOT_DISABLE_DAEMON") != "1"
    ):
        ensure_daemon(store)
    return inserted


def main() -> int:
    ensure_data_dir()
    logger = configure_logging("codexbot.hooks")
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        process_hook(payload, Store(database_path()))
    except Exception as exc:
        # Do not log payloads: prompts and final replies may contain private data.
        detail = redact_secrets(str(exc))[:300]
        logger.error("Hook processing failed: %s: %s", type(exc).__name__, detail)
    sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
