"""When the customer types an order number, look it up and prompt Aria to speak the result."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from pipecat.frames.frames import Frame, LLMMessagesAppendFrame, LLMRunFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.frames import InboundTypedOrderFrame

logger = logging.getLogger(__name__)


class InboundTypedOrderProcessor(FrameProcessor):
    """
    Customer tapped Send on the order-number box.

    Fetches order status immediately and nudges Gemini to speak the full answer
    (avoids "let me check" then silence when no user speech follows).
    """

    def __init__(self, llm, **kwargs) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._lookup_in_flight = False
        self._last_lookup_key = ""
        self._last_lookup_at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not isinstance(frame, InboundTypedOrderFrame):
            await self.push_frame(frame, direction)
            return

        key = f"{frame.session_id}:{frame.order_id}"
        now = time.monotonic()
        if self._lookup_in_flight or (
            key == self._last_lookup_key and now - self._last_lookup_at < 2.0
        ):
            return

        self._lookup_in_flight = True
        self._last_lookup_key = key
        self._last_lookup_at = now
        asyncio.create_task(self._lookup_and_respond(frame.order_id))

    async def _lookup_and_respond(self, order_id: str) -> None:
        try:
            from app.services.order_service import order_service

            logger.info("Typed order lookup starting order=%s", order_id)
            result = await order_service.get_order_status(order_id)
            payload = json.dumps(result, ensure_ascii=False)
            if len(payload) > 3500:
                payload = payload[:3500] + "…"

            if result.get("found"):
                hint = (
                    f"Tell them what you found for order {result.get('order_id', order_id)}: "
                    "status, main items, total if present, and delivery address. "
                    "One concise spoken reply — do not stop after only 'let me check'."
                )
            else:
                hint = (
                    "Say the order was not found and ask them to re-check the number in the box "
                    "and tap Send again. Do not stop after only 'let me check'."
                )

            instruction = (
                "[System — not the customer] The customer typed their order number in the "
                f"on-screen box and tapped Send: {order_id}. "
                "Order lookup already completed (JSON below). "
                f"{hint}\n{payload}"
            )
            await self._llm.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": instruction}],
                ),
                FrameDirection.DOWNSTREAM,
            )
            await self._llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
            logger.info(
                "Typed order lookup done order=%s found=%s",
                order_id,
                result.get("found"),
            )
        except Exception:
            logger.exception("Typed order lookup failed order=%s", order_id)
            try:
                await self._llm.queue_frame(
                    LLMMessagesAppendFrame(
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    "[System — not the customer] Order lookup failed. "
                                    f"Apologise briefly and ask them to try order {order_id} "
                                    "in the box again in a moment."
                                ),
                            }
                        ],
                    ),
                    FrameDirection.DOWNSTREAM,
                )
                await self._llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
            except Exception:
                logger.exception("Failed to nudge LLM after typed-order error")
        finally:
            self._lookup_in_flight = False
