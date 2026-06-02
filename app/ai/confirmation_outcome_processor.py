"""Capture customer speech during outbound confirmation calls."""

from __future__ import annotations

import asyncio
import logging

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.services.call_context_store import get_context, save_context
from app.services.confirmation_factory import get_order_confirmation_service

logger = logging.getLogger(__name__)


class ConfirmationOutcomeProcessor(FrameProcessor):
    """Accumulate user transcriptions and finalize Shopify outcome when call ends."""

    def __init__(self, session_id: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session_id = session_id
        self._finalized = False

    def _note_transcription(self, text: str) -> None:
        ctx = get_context(self._session_id)
        if not ctx or not text or not text.strip():
            return
        cleaned = text.strip()
        ctx.user_transcripts.append(cleaned)
        ctx.customer_answered = True
        save_context(ctx)

    async def _finalize(self, *, force_no_answer: bool = False) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            await get_order_confirmation_service().finalize_call(
                self._session_id,
                force_no_answer=force_no_answer,
            )
        except Exception:
            logger.exception("Failed to finalize confirmation session=%s", self._session_id)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Pass frames through immediately (same low-latency path as inbound).
        await self.push_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            asyncio.create_task(self._finalize())
        elif (
            direction == FrameDirection.UPSTREAM
            and isinstance(frame, TranscriptionFrame)
            and frame.text
        ):
            text = frame.text.strip()
            asyncio.create_task(asyncio.to_thread(self._note_transcription, text))
