from __future__ import annotations

import unicodedata

from codexbot.formatting import bisect_segment, render_segment, split_text


def test_split_preserves_chinese_emoji_and_every_newline() -> None:
    source = ("中文🙂👨‍👩‍👧‍👦e\u0301\n\n下一行 " * 100) + "结束"
    chunks = split_text(source, limit=120)

    assert "".join(chunks) == source
    assert len(chunks) > 2
    for index, chunk in enumerate(chunks):
        assert len(render_segment(chunks, index)) <= 120
        if index:
            assert not unicodedata.combining(chunk[0])
            assert chunk[0] not in {"\u200d", "\ufe0e", "\ufe0f"}
        if index < len(chunks) - 1:
            assert not chunk.endswith("\u200d")


def test_split_preserves_windows_newlines_without_losing_content() -> None:
    source = "a\r\n\r\nb\rc"
    assert "".join(split_text(source, limit=80)) == source


def test_bisect_preserves_the_exact_segment() -> None:
    source = "甲" * 100 + "\n\n" + "乙🙂" * 100
    left, right = bisect_segment(source)

    assert left + right == source
    assert left
    assert right
