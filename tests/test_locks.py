from __future__ import annotations

from pathlib import Path

from codexbot.locks import FileLock


def test_user_singleton_lock(tmp_path: Path) -> None:
    first = FileLock(tmp_path / "single.lock")
    second = FileLock(tmp_path / "single.lock")

    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()

