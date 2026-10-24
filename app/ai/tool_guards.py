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


def resolve_order_id_for_tool(
    raw: str,
    *,
    data_source: str | None = None,
    inbound_session_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """
    Normalize order id for tools. Falls back to customer-typed id when speech was missing/invalid.
    """
    from app.services.inbound_session_store import get_typed_order_id

    cleaned = normalize_order_id(raw)
    err = reject_tool_without_customer_order_id(cleaned, data_source=data_source)
    if err is None:
        return cleaned, None

    typed = get_typed_order_id(inbound_session_id)
    if typed:
        typed_clean = normalize_order_id(typed)
        err2 = reject_tool_without_customer_order_id(
            typed_clean, data_source=data_source
        )
        if err2 is None:
            return typed_clean, None
    return cleaned, err


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
                "You do not have an order number yet. Tell the customer once: "
                "please type their order number in the box on screen and tap Send. "
                'Do NOT ask "What is your order ID?" Do NOT guess or invent a number.'
            ),
        }
    if ds == "magento":
        if not _MAGENTO_ORDER_ID.match(cleaned):
            return {
                "found": False,
                "error": "invalid_order_id",
                "instruction_for_agent": (
                    f"'{order_id}' is not a valid Magento order number. "
                    "Ask them to type the correct order number in the box on screen and tap Send, "
                    "or say the digits clearly."
                ),
            }
        return None

    if not _ORDER_DIGITS.match(cleaned):
        return {
            "found": False,
            "error": "invalid_order_id",
            "instruction_for_agent": (
                f"'{order_id}' is not a valid order number format. "
                "Ask them to type the order number in the box on screen and tap Send, "
                "or say the digits clearly."
            ),
        }
    return None
