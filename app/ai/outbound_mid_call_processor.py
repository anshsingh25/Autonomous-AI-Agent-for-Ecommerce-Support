"""Prevent outbound calls from restarting opening after tools or mid-call hello."""

from __future__ import annotations

import logging
import time

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from app.ai.outbound_call_state import (
    handle_mid_call_follow_up,
    handle_mid_call_hello,
    transcript_is_mid_call_hello,
    transcript_needs_follow_up,
)
from app.services.call_context_store import get_context, save_context

logger = logging.getLogger(__name__)


class OutboundMidCallProcessor(FrameProcessor):
    """Answer follow-ups and block accidental call restarts after address updates."""

    def __init__(
        self,
        llm: GeminiLiveLLMService,
        session_id: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._session_id = session_id
        self._started_at = time.monotonic()
        self._last_nudge_at = 0.0

    async def _maybe_handle(self, text: str) -> None:
        ctx = get_context(self._session_id)
        if not ctx:
            return
        cleaned = (text or "").strip()
        if not cleaned:
            return
        ctx.user_transcripts.append(cleaned)
        if len(ctx.user_transcripts) > 30:
            ctx.user_transcripts = ctx.user_transcripts[-30:]

        # Auto-mark opening done after ~30s or substantive utterance.
        if not ctx.opening_complete and (
            time.monotonic() - self._started_at > 30
            or len(cleaned.split()) > 8
        ):
            ctx.opening_complete = True
            save_context(ctx)

        now = time.monotonic()
        if now - self._last_nudge_at < 0.8:
            return

        handled = False
        if ctx.opening_complete or time.monotonic() - self._started_at > 20:
            if transcript_needs_follow_up(cleaned):
                handled = await handle_mid_call_follow_up(self._llm, ctx, cleaned)
            elif transcript_is_mid_call_hello(cleaned):
                handled = await handle_mid_call_hello(self._llm, ctx, cleaned)

        if handled:
            self._last_nudge_at = now
            save_context(ctx)
            logger.info("Outbound mid-call guard handled: %r", cleaned[:80])
            return

        if ctx.opening_complete and len(ctx.user_transcripts) >= 5:
            from app.services.confirmation_intent import (
                customer_final_order_confirm,
                detect_confirmation_intent,
            )
            from app.models.call_outcome import CallOutcome

            if customer_final_order_confirm(cleaned):
                ctx.final_confirm_received = True
                save_context(ctx)
                logger.info("Outbound: final order confirm detected — hangup after closing")
            elif detect_confirmation_intent(cleaned) == CallOutcome.CANCELLED:
                ctx.final_confirm_received = True
                save_context(ctx)
                logger.info("Outbound: cancel intent detected — hangup after closing")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, TranscriptionFrame)
            and frame.text
            and direction == FrameDirection.UPSTREAM
        ):
            await self._maybe_handle(frame.text)
        await self.push_frame(frame, direction)
