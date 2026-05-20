"""Spoken hold lines while order tools run — avoids awkward silence on calls."""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Any

from pipecat.frames.frames import LLMMessagesAppendFrame, LLMRunFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams

logger = logging.getLogger(__name__)

# Spoken once per tool call before the backend request runs.
_TOOL_HOLD_LINES: dict[str, str] = {
    "get_order_status": (
        "Please give me just a moment — I'm pulling up your order details now."
    ),
    "track_order": (
        "One second — I'm checking tracking and delivery on your order."
    ),
    "refund_status": (
        "Please hold on — I'm checking your refund status right now."
    ),
    "cancel_order": (
        "Alright — please wait a moment while I process the cancellation on your order."
    ),
}

# When update_* tools are added, extend this map.
_UPDATE_HOLD_HINTS: tuple[tuple[str, str], ...] = (
    ("address", "Please hold on — I'm updating your delivery address now."),
    ("quantity", "Just a moment — I'm updating the quantity on your order."),
    ("qty", "Just a moment — I'm updating the quantity on your order."),
    ("phone", "One sec — I'm updating your phone number on the order."),
    ("email", "Please wait — I'm updating your email on the account."),
    ("size", "Give me a moment — I'm updating the size on your order."),
    ("variant", "Please hold on — I'm swapping the item on your order now."),
    ("swap", "Please hold on — I'm changing the size and colour on your order."),
)

_OUTBOUND_TOOL_HOLD: dict[str, str] = {
    "list_item_variants": "One moment — I'm checking which sizes and colours are in stock.",
    "swap_item_variant": "Please hold on — I'm updating the size and colour on your order.",
    "update_delivery_address": "Please hold on — I'm updating your delivery address now.",
    "update_item_quantity": "Just a moment — I'm updating the quantity on your order.",
    "refresh_confirmation_order": "One second — I'm refreshing your order details.",
    "cancel_confirmation_order": "Alright — I'm processing the cancellation now.",
}


def hold_line_for_tool(function_name: str, arguments: Mapping[str, Any] | None = None) -> str:
    """Pick a customer-facing hold line for this tool / action."""
    if function_name in _OUTBOUND_TOOL_HOLD:
        return _OUTBOUND_TOOL_HOLD[function_name]
    if function_name in _TOOL_HOLD_LINES:
        return _TOOL_HOLD_LINES[function_name]

    args = arguments or {}
    blob = " ".join(str(v).lower() for v in args.values())
    for keyword, line in _UPDATE_HOLD_HINTS:
        if keyword in blob or keyword in function_name.lower():
            return line

    if function_name.startswith("update_"):
        field = function_name.replace("update_", "").replace("_", " ")
        return f"Please hold on — I'm updating your {field} now."

    return "Please give me just a moment — I'm working on that in our system."


async def speak_tool_hold(
    params: FunctionCallParams,
    *,
    pause_before_work_sec: float = 0.9,
) -> None:
    """
    Ask Gemini to say a short hold line before a tool/API call finishes.

    A brief pause lets audio start so the customer is not left in silence.
    """
    line = hold_line_for_tool(params.function_name, params.arguments)
    llm = params.llm

    try:
        await llm.queue_frame(
            LLMMessagesAppendFrame(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "[System — not the customer] The customer is waiting on the phone. "
                            f'Say ONLY this aloud right now, warmly and naturally, then stay quiet '
                            f'until you have the tool result: "{line}"'
                        ),
                    }
                ],
            ),
            FrameDirection.DOWNSTREAM,
        )
        await llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
        if pause_before_work_sec > 0:
            await asyncio.sleep(pause_before_work_sec)
    except Exception:
        logger.exception("Failed to queue tool hold message for %s", params.function_name)
