"""Shared parsing helpers for voice tool arguments."""

from __future__ import annotations

import re

_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def parse_quantity(raw: object) -> int | None:
    """Parse numeric or spoken quantity (e.g. 3, '3', 'three')."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in _NUMBER_WORDS:
        return _NUMBER_WORDS[text]
    match = re.search(r"\b(\d{1,2})\b", text)
    if match:
        return int(match.group(1))
    return None
