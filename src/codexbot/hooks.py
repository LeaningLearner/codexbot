from __future__ import annotations

import json
import os
import sys
from typing import Iterable

from .logging_utils import configure_logging
from .paths import database_path, ensure_data_dir
from .processes import discover_codex_host, ensure_daemon
from .security import redact_secrets
from .store import Store


ANCESTOR_PIDS_ENV = "CODEXBOT_HOOK_ANCESTOR_PIDS"


def _ancestor_pids_from_environment() -> tuple[int, ...]:
    raw = os.environ.pop(ANCESTOR_PIDS_ENV, "")
    pids: list[int] = []
    for value in raw.split(",")[:32]:
        try:
            pid = int(value.strip())
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return tuple(pids)


def process_hook(
    payload: dict[str, object],
    store: Store,
    *,
    ancestor_pids: Iterable[int] | None = None,
) -> bool:
    candidates = (
        _ancestor_pids_from_environment()
        if ancestor_pids is None
        else tuple(ancestor_pids)
    )
    host = discover_codex_host(ancestor_pids=candidates)
    inserted = store.ingest_hook(payload, host)
    event_name = payload.get("hook_event_name")
    lifecycle_host = host is not None and event_name != "SessionEnd"
    if (
        os.environ.get("CODEXBOT_DISABLE_DAEMON") != "1"
        and (lifecycle_host or store.companion_work_pending())
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
