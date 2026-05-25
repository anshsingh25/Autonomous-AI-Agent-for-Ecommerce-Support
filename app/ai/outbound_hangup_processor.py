"""Hang up outbound telephony calls after the agent's closing statement."""

from __future__ import annotations

import asyncio
import logging
import re

from pipecat.frames.frames import (
    AggregatedTextFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

_CLOSING_PHRASES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"thank you for your time",
        r"thank you so much",
        r"have a (?:wonderful|great|lovely|nice) day",
        r"your order is confirmed",
        r"order is confirmed",
        r"processed for delivery soon",
        r"will be processed for delivery",
        r"follow[- ]?up call shortly",
        r"cancellation request has been processed",
        r"confirm ho gaya",
        r"dhanyavaad",
        r"shukriya",
        r"goodbye",
        r"bye bye",
    )
)

# Agent still collecting info — never treat as call ending.
_MID_CALL_QUESTION = re.compile(
    r"(?:\?|\b(full address|delivery address|complete address|pin code|zip code|"
    r"postal code|what is your|please tell|please share|bataiye|batayein|"
    r"kya hai aapka|update.*address)\b)",
    re.IGNORECASE,
)

_POST_CONFIRM_CLOSING = re.compile(
    r"\b(thank you|thank you for your time|shukriya|dhanyavaad|have a (?:great|wonderful|nice) day|"
    r"confirmed|processed for delivery|cancellation request)\b",
    re.IGNORECASE,
)

_MIN_CLOSING_UTTERANCE_LEN = 20
# Gemini output transcription may arrive after BotStoppedSpeaking on PSTN.
_POST_STOP_TEXT_WAIT_SEC = 0.45


def utterance_looks_like_call_closing(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return False
    if _MID_CALL_QUESTION.search(cleaned):
        return False
    if re.search(r"\bthank you for your time\b", cleaned, re.I) and len(cleaned) >= 15:
        return True
    if len(cleaned) < _MIN_CLOSING_UTTERANCE_LEN:
        return False
    return any(p.search(cleaned) for p in _CLOSING_PHRASES)


def _should_hangup_on_agent_text(text: str, *, final_confirm_received: bool) -> bool:
    if utterance_looks_like_call_closing(text):
        return True
    if not final_confirm_received or not text.strip():
        return False
    cleaned = re.sub(r"\s+", " ", text.strip())
    if _MID_CALL_QUESTION.search(cleaned):
        return False
    # After explicit final confirm, accept shorter goodbye / confirm lines.
    return bool(_POST_CONFIRM_CLOSING.search(cleaned)) and len(cleaned) >= 12


class OutboundHangupOnCloseProcessor(FrameProcessor):
    """
    End the call after the agent's closing line.

    PSTN note: Gemini output transcription often lands after BotStoppedSpeaking,
    so we defer the hangup decision briefly and keep a rolling agent-text buffer.
    """

    def __init__(
        self,
        task_holder: list[PipelineTask | None],
        *,
        session_id: str | None = None,
        hangup_delay_sec: float = 1.5,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._task_holder = task_holder
        self._session_id = session_id or ""
        self._hangup_delay_sec = hangup_delay_sec
        self._turn_text = ""
        self._recent_agent_text = ""
        self._hangup_scheduled = False
        self._evaluate_task: asyncio.Task | None = None

    def _ctx_final_confirm(self) -> bool:
        if not self._session_id:
            return False
        from app.services.call_context_store import get_context

        ctx = get_context(self._session_id)
        if not ctx:
            return False
        return bool(
            getattr(ctx, "final_confirm_received", False)
            or getattr(ctx, "shopify_cancel_via_tool", False)
        )

    def _best_agent_text(self) -> str:
        turn = self._turn_text.strip()
        if turn:
            return turn
        return self._recent_agent_text.strip()[-500:]

    async def _hangup_after_delay(self) -> None:
        try:
            await asyncio.sleep(self._hangup_delay_sec)
            task = self._task_holder[0] if self._task_holder else None
            if not task or task.has_finished():
                return
            logger.info(
                "Outbound: hanging up call after closing statement (delay=%.1fs)",
                self._hangup_delay_sec,
            )
            await task.stop_when_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbound: failed to hang up call after closing")

    def _schedule_hangup(self, *, heard: str, reason: str) -> None:
        if self._hangup_scheduled:
            return
        self._hangup_scheduled = True
        logger.info(
            "Outbound: hangup scheduled (%s) in %.1fs — %r",
            reason,
            self._hangup_delay_sec,
            heard[:120],
        )
        asyncio.create_task(self._hangup_after_delay())

    def _note_agent_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._turn_text += chunk
        self._recent_agent_text = (self._recent_agent_text + chunk)[-800:]

    def _cancel_evaluate_task(self) -> None:
        if self._evaluate_task and not self._evaluate_task.done():
            self._evaluate_task.cancel()
        self._evaluate_task = None

    async def _evaluate_hangup_after_stop(self, *, reason: str) -> None:
        try:
            await asyncio.sleep(_POST_STOP_TEXT_WAIT_SEC)
            text = self._best_agent_text()
            if _should_hangup_on_agent_text(
                text, final_confirm_received=self._ctx_final_confirm()
            ):
                self._schedule_hangup(heard=text, reason=reason)
            else:
                logger.debug(
                    "Outbound: no hangup after %s (final_confirm=%s text=%r)",
                    reason,
                    self._ctx_final_confirm(),
                    text[:100],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbound: hangup evaluation failed")

    def _queue_hangup_evaluation(self, *, reason: str) -> None:
        self._cancel_evaluate_task()
        self._evaluate_task = asyncio.create_task(
            self._evaluate_hangup_after_stop(reason=reason)
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._hangup_scheduled:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            self._cancel_evaluate_task()
            self._turn_text = ""
        elif isinstance(frame, (LLMTextFrame, TTSTextFrame, AggregatedTextFrame)):
            self._note_agent_text(getattr(frame, "text", None) or "")
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = self._best_agent_text()
            if _should_hangup_on_agent_text(
                text, final_confirm_received=self._ctx_final_confirm()
            ):
                self._schedule_hangup(heard=text, reason="response_end")
        elif isinstance(frame, (BotStoppedSpeakingFrame, TTSStoppedFrame)):
            self._queue_hangup_evaluation(reason="bot_stopped")

        await self.push_frame(frame, direction)
