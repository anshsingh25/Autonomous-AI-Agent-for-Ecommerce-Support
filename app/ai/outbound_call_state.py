"""Mid-call state and LLM reminders for outbound confirmation (no restart after tools)."""

from __future__ import annotations

import logging
import re

from pipecat.frames.frames import LLMMessagesAppendFrame, LLMRunFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from app.models.call_context import OutboundCallContext
from app.services.call_context_store import save_context

logger = logging.getLogger(__name__)

_FOLLOW_UP_UPDATE = re.compile(
    r"(?:did you|have you|address).{0,30}(?:update|change|save|done|ho gaya|kar diya|hogya|huva|hua)|"
    r"(?:update|change).{0,20}(?:done|ho gaya|kar diya|hogya)|"
    r"ho gaya\??|kar diya\??|update hua",
    re.IGNORECASE,
)

_HELLO_ONLY = re.compile(
    r"^\s*(?:hello|hi|hey|namaste|hallo|helo)\s*[.!?]?\s*$",
    re.IGNORECASE,
)


async def inject_llm_reminder(
    llm: GeminiLiveLLMService,
    content: str,
    *,
    run: bool = True,
) -> None:
    await llm.queue_frame(
        LLMMessagesAppendFrame(messages=[{"role": "user", "content": content}]),
        FrameDirection.DOWNSTREAM,
    )
    if run:
        await llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)


def record_tool_outcome(
    ctx: OutboundCallContext,
    tool_name: str,
    result: dict,
) -> str | None:
    """Persist last tool result on call context for follow-up questions."""
    if not isinstance(result, dict):
        return None
    success = bool(result.get("success"))
    ctx.last_tool_name = tool_name
    ctx.last_tool_success = success
    if success and tool_name == "update_delivery_address":
        addr = result.get("delivery_address") or (result.get("verified_order") or {}).get(
            "delivery_address"
        )
        ctx.delivery_address = str(addr or ctx.delivery_address or "")
        ctx.last_tool_summary = (
            f"Delivery address saved to order desk: {ctx.delivery_address or 'updated'}."
        )
    elif success and tool_name == "update_item_quantity":
        items = result.get("items") or (result.get("verified_order") or {}).get("items")
        ctx.last_tool_summary = f"Quantity updated. Items: {items}. Total ₹{result.get('total_inr', ctx.total_inr)}."
    elif success and result.get("shopify_updated") is False:
        ctx.last_tool_summary = str(
            result.get("message")
            or "Saved locally only — Shopify Admin was not updated."
        )
    elif success:
        ctx.last_tool_summary = str(result.get("message") or f"{tool_name} completed successfully.")
    else:
        ctx.last_tool_summary = str(
            result.get("message") or f"{tool_name} failed — tell customer and retry or escalate."
        )
    save_context(ctx)
    return ctx.last_tool_summary


async def remind_after_tool(
    llm: GeminiLiveLLMService,
    ctx: OutboundCallContext,
    tool_name: str,
    result: dict,
) -> None:
    """Tell the model what just happened so it can answer 'did you update?' later."""
    summary = record_tool_outcome(ctx, tool_name, result)
    if not summary:
        return
    if not result.get("success"):
        await inject_llm_reminder(
            llm,
            "[System — not the customer] The last action failed. "
            f"{summary} Explain briefly to the customer and offer to try again. "
            "Do NOT repeat the call opening or language question.",
            run=False,
        )
        return

    shopify_ok = result.get("shopify_updated") is not False and result.get("shopify_sync") != "failed"
    speak_hint = ""
    if tool_name == "update_delivery_address" and shopify_ok:
        speak_hint = (
            "If you have not already confirmed aloud, say ONE short line now that the "
            "address is saved (city + PIN), then ask if they want any other change."
        )
    elif not shopify_ok:
        speak_hint = (
            "Tell the customer the change is noted for the team but Shopify may not show it yet — "
            "do NOT claim the order is fully updated in the system."
        )

    await inject_llm_reminder(
        llm,
        "[System — not the customer] Tool finished successfully. "
        f"{summary} {speak_hint} "
        "If the customer later asks 'did you update?' / 'ho gaya?' — answer honestly from this result "
        "(order desk JSON vs Shopify Admin). NEVER restart with 'Hi this is Aria' or language question again. "
        "Mark call phase as changes or final-confirm, not opening.",
        run=False,
    )
    ctx.opening_complete = True
    save_context(ctx)


def transcript_needs_follow_up(text: str) -> bool:
    return bool(_FOLLOW_UP_UPDATE.search((text or "").strip()))


def transcript_is_mid_call_hello(text: str) -> bool:
    return bool(_HELLO_ONLY.match((text or "").strip()))


async def handle_mid_call_follow_up(
    llm: GeminiLiveLLMService,
    ctx: OutboundCallContext,
    heard: str,
) -> bool:
    """Inject answer for 'did you update the address?' — returns True if handled."""
    if not transcript_needs_follow_up(heard):
        return False
    if ctx.last_tool_summary and ctx.last_tool_name == "update_delivery_address":
        answer = (
            f"[System — not the customer] Customer asked if the address was updated. "
            f"Answer directly in one sentence: Yes — {ctx.last_tool_summary} "
            "Do NOT repeat call opening or language preference."
        )
    elif ctx.last_tool_summary:
        answer = (
            f"[System — not the customer] Customer asked about the last change. "
            f"Answer: {ctx.last_tool_summary} Do NOT restart the call opening."
        )
    else:
        answer = (
            "[System — not the customer] Customer asked about an update but no tool result "
            "is on record. Say you are checking, call refresh_confirmation_order, then answer. "
            "Do NOT repeat call opening."
        )
    await inject_llm_reminder(llm, answer)
    return True


async def handle_mid_call_hello(
    llm: GeminiLiveLLMService,
    ctx: OutboundCallContext,
    heard: str,
) -> bool:
    if not transcript_is_mid_call_hello(heard):
        return False
    if not ctx.opening_complete:
        return False
    await inject_llm_reminder(
        llm,
        "[System — not the customer] Customer said hello mid-call (check-in). "
        f"Reply briefly: you are still on the line from badlavo about order {ctx.order_id}. "
        "Continue from the current step (address change / confirmation). "
        "Do NOT say the full opening intro or ask English/Hindi again.",
    )
    return True
