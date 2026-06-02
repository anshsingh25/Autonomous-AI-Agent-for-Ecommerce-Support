"""Validation so the voice agent cannot look up invented order numbers."""

from __future__ import annotations

import re
from typing import Any

_ORDER_DIGITS = re.compile(r"^\d{3,12}$")
_MAGENTO_ORDER_ID = re.compile(r"^[A-Za-z0-9-]{3,24}$")
_WORD_TO_DIGIT = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
}


def normalize_order_id(raw: str) -> str:
    text = raw.strip().lstrip("#")
    direct_digits = "".join(ch for ch in text if ch.isdigit())
    if len(direct_digits) >= 3:
        return direct_digits

    # Handle spoken digit words: "one five six nine" -> "1569"
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    spoken_digits = "".join(_WORD_TO_DIGIT.get(token, "") for token in tokens)
    if len(spoken_digits) >= 3:
        return spoken_digits

    return text.replace(" ", "").upper()


def reject_tool_without_customer_order_id(
    order_id: str,
    *,
    data_source: str | None = None,
) -> dict[str, Any] | None:
    """
    Return an error payload for the LLM if order_id was missing or invalid.
    The agent must only use numbers the customer said aloud.
    """
    from app.utils.config import get_settings

    ds = (data_source or get_settings().data_source).lower().strip()
    cleaned = normalize_order_id(order_id)
    if not cleaned:
        return {
            "found": False,
            "error": "missing_order_id",
            "instruction_for_agent": (
                "You do not have an order number from the customer yet. "
                "Ask: What is your order number? Do NOT guess or invent a number. "
                "Do NOT call this tool again until they say digits clearly."
            ),
        }
    if ds == "magento":
        if not _MAGENTO_ORDER_ID.match(cleaned):
            return {
                "found": False,
                "error": "invalid_order_id",
                "instruction_for_agent": (
                    f"'{order_id}' is not a valid Magento order number. "
                    "Use the increment id the customer stated (digits, e.g. 000000123). "
                    "Ask them to repeat the order number."
                ),
            }
        return None

    if not _ORDER_DIGITS.match(cleaned):
        return {
            "found": False,
            "error": "invalid_order_id",
            "instruction_for_agent": (
                f"'{order_id}' is not a valid order number format. "
                "Only use digits the customer stated. Ask them to repeat the order number."
            ),
        }
    return None
