from __future__ import annotations

import io
from pathlib import Path

import codexbot.hooks as hooks


def test_hook_main_redacts_exception_details(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    messages: list[str] = []

    class Logger:
        def error(self, message: str, *args: object) -> None:
            messages.append(message % args)

    class FakeStdin:
        buffer = io.BytesIO(b"{}")

    def broken_process(_payload: dict[str, object], _store: object) -> bool:
        raise RuntimeError("hook api_key=hook-secret-value")

    monkeypatch.setattr(hooks, "ensure_data_dir", lambda: tmp_path)
    monkeypatch.setattr(hooks, "database_path", lambda: tmp_path / "state.sqlite3")
    monkeypatch.setattr(hooks, "configure_logging", lambda _name: Logger())
    monkeypatch.setattr(hooks, "process_hook", broken_process)
    monkeypatch.setattr(hooks.sys, "stdin", FakeStdin())

    assert hooks.main() == 0
    assert messages
    assert "hook-secret-value" not in messages[0]
    assert "[REDACTED]" in messages[0]
