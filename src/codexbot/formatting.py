from __future__ import annotations

import unicodedata


DEFAULT_CHUNK_SIZE = 1500


def _safe_cut(text: str, target: int) -> int:
    target = min(max(1, target), len(text))
    while target > 1 and target < len(text):
        current = text[target]
        previous = text[target - 1]
        if (
            unicodedata.combining(current)
            or current == "\u200d"
            or previous == "\u200d"
            or current in {"\ufe0e", "\ufe0f"}
            or previous in {"\ufe0e", "\ufe0f"}
        ):
            target -= 1
            continue
        break
    return target


def split_text(text: str, limit: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    text = text or ""
    if not text:
        return [""]
    if limit < 80:
        raise ValueError("limit must be at least 80")

    payload_limit = limit - 20
    chunks: list[str] = []
    remaining = text
    while len(remaining) > payload_limit:
        cut = _safe_cut(remaining, payload_limit)
        newline = remaining.rfind("\n", max(0, cut - 300), cut)
        whitespace = remaining.rfind(" ", max(0, cut - 120), cut)
        if newline > 0:
            cut = newline + 1
        elif whitespace > 0:
            cut = whitespace + 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    chunks.append(remaining)
    return chunks


def render_segment(segments: list[str], index: int) -> str:
    if not 0 <= index < len(segments):
        raise IndexError(index)
    if len(segments) == 1:
        return segments[index]
    return f"[{index + 1}/{len(segments)}]\n{segments[index]}"


def bisect_segment(segment: str) -> tuple[str, str]:
    if len(segment) < 2:
        raise ValueError("segment cannot be split further")
    cut = _safe_cut(segment, len(segment) // 2)
    newline = segment.rfind("\n", max(1, cut - 150), min(len(segment), cut + 150))
    if newline > 0:
        cut = newline + 1
    return segment[:cut], segment[cut:]
