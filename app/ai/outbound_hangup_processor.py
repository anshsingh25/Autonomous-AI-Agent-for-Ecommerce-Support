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
        r"thank you",
        r"have a (?:wonderful|great|lovely|nice) day",
        r"your order is confirmed",
        r"order is confirmed",
        r"order confirm",
        r"processed for delivery soon",
        r"will be processed for delivery",
        r"follow[- ]?up call shortly",
        r"cancellation request has been processed",
        r"cancel(?:led|lation).*(?:processed|done)",
        r"confirm ho gaya",
        r"order confirm ho",
        r"dhanyavaad",
        r"dhanyawad",
        r"shukriya",
        r"alvida",
        r"goodbye",
        r"bye bye",
        r"call back",
    )
)

# Agent still collecting info — never treat as call ending.
_MID_CALL_QUESTION = re.compile(
    r"(?:\?|\b(full address|delivery address|complete address|pin code|zip code|"
    r"postal code|what is your|please tell|please share|bataiye|batayein|"
    r"kya hai aapka|update.*address|anything else to change|kuch aur change)\b)",
    re.IGNORECASE,
)

_POST_CONFIRM_CLOSING = re.compile(
    r"\b(thank you|thank you for your time|shukriya|dhanyavaad|dhanyawad|"
    r"have a (?:great|wonderful|nice) day|confirmed|processed for delivery|"
    r"cancellation request|confirm ho gaya|order confirm)\b",
    re.IGNORECASE,
)

# Primary rule: agent said thank you → end call (after opening, not mid address Q).
_AGENT_THANK_YOU = re.compile(
    r"\b(thank you|thanks|thank u|shukriya|dhanyavaad|dhanyawad|धन्यवाद)\b",
    re.IGNORECASE,
)

_MIN_CLOSING_UTTERANCE_LEN = 18
_THANK_YOU_HANGUP_DELAY_SEC = 1.0
# Gemini output transcription often lands after BotStoppedSpeaking / turnComplete.
_POST_STOP_TEXT_WAIT_SEC = 0.95


def agent_said_thank_you(text: str) -> bool:
    """True when the agent's speech includes a goodbye thank-you (hang up immediately)."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) < 6:
        return False
    if _MID_CALL_QUESTION.search(cleaned):
        return False
    return bool(_AGENT_THANK_YOU.search(cleaned))


def utterance_looks_like_call_closing(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return False
    if _MID_CALL_QUESTION.search(cleaned):
        return False
    if re.search(r"\bthank you for your time\b", cleaned, re.I) and len(cleaned) >= 12:
        return True
    if re.search(r"\b(thank you|shukriya|dhanyavaad)\b", cleaned, re.I) and len(cleaned) >= 10:
        return True
    if len(cleaned) < _MIN_CLOSING_UTTERANCE_LEN:
        return False
    return any(p.search(cleaned) for p in _CLOSING_PHRASES)


def _should_hangup_on_agent_text(text: str, *, final_confirm_received: bool) -> bool:
    if agent_said_thank_you(text):
        return True
    if utterance_looks_like_call_closing(text):
        return True
    if not final_confirm_received or not text.strip():
        return False
    cleaned = re.sub(r"\s+", " ", text.strip())
    if _MID_CALL_QUESTION.search(cleaned):
        return False
    return bool(_POST_CONFIRM_CLOSING.search(cleaned)) and len(cleaned) >= 10


def _should_force_hangup_after_final_confirm(
    text: str,
    *,
    agent_turns_after_final_confirm: int,
) -> bool:
    """
    After the customer confirmed/cancelled, disconnect once the agent finishes
    a non-question turn (even if closing text was not transcribed on PSTN).
    """
    if agent_turns_after_final_confirm < 1:
        return False
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if cleaned and _MID_CALL_QUESTION.search(cleaned):
        return False
    if cleaned and utterance_looks_like_call_closing(cleaned):
        return True
    if cleaned and _POST_CONFIRM_CLOSING.search(cleaned):
        return True
    # No transcript on wire — still end after one completed agent turn post-confirm.
    return not cleaned or "?" not in cleaned


class OutboundHangupOnCloseProcessor(FrameProcessor):
    """
    End the call after the agent's closing line.

    PSTN note: Gemini output transcription often lands after BotStoppedSpeaking,
    so we defer the hangup decision and read agent speech from call context too.
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

    def _load_ctx(self):
        if not self._session_id:
            return None
        from app.services.call_context_store import get_context

        return get_context(self._session_id)

    def _ctx_final_confirm(self) -> bool:
        ctx = self._load_ctx()
        if not ctx:
            return False
        return bool(
            getattr(ctx, "final_confirm_received", False)
            or getattr(ctx, "shopify_cancel_via_tool", False)
        )

    def _ctx_agent_turns_after_confirm(self) -> int:
        ctx = self._load_ctx()
        if not ctx:
            return 0
        return int(getattr(ctx, "agent_turns_after_final_confirm", 0) or 0)

    def _ctx_ready_for_thank_you_hangup(self) -> bool:
        """Avoid hanging up on a stray thank-you during the opening."""
        ctx = self._load_ctx()
        if not ctx:
            return False
        if getattr(ctx, "opening_complete", False):
            return True
        return len(getattr(ctx, "user_transcripts", []) or []) >= 3

    def _best_agent_text(self) -> str:
        turn = self._turn_text.strip()
        if turn:
            return turn
        recent = self._recent_agent_text.strip()
        if recent:
            return recent[-500:]
        ctx = self._load_ctx()
        if ctx and getattr(ctx, "recent_agent_speech", ""):
            return str(ctx.recent_agent_speech).strip()[-500:]
        return ""

    def _schedule_hangup(self, *, heard: str, reason: str, delay_sec: float | None = None) -> None:
        if self._hangup_scheduled:
            return
        self._hangup_scheduled = True
        delay = self._hangup_delay_sec if delay_sec is None else delay_sec
        self._pending_hangup_delay = delay
        logger.info(
            "Outbound: hangup scheduled (%s) in %.1fs — %r",
            reason,
            delay,
            heard[:120],
        )
        asyncio.create_task(self._hangup_after_delay(delay))

    async def _hangup_after_delay(self, delay_sec: float | None = None) -> None:
        try:
            delay = (
                delay_sec
                if delay_sec is not None
                else getattr(self, "_pending_hangup_delay", self._hangup_delay_sec)
            )
            await asyncio.sleep(delay)
            task = self._task_holder[0] if self._task_holder else None
            if not task or task.has_finished():
                return
            logger.info(
                "Outbound: hanging up call after closing statement (delay=%.1fs)",
                delay,
            )
            await task.stop_when_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbound: failed to hang up call after closing")

    def _note_agent_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._turn_text += chunk
        self._recent_agent_text = (self._recent_agent_text + chunk)[-800:]
        self._maybe_hangup_on_thank_you()

    def _maybe_hangup_on_thank_you(self) -> None:
        """End call as soon as thank-you appears in agent speech (live transcription)."""
        if self._hangup_scheduled or not self._ctx_ready_for_thank_you_hangup():
            return
        text = self._best_agent_text()
        if agent_said_thank_you(text):
            self._schedule_hangup(
                heard=text,
                reason="thank_you",
                delay_sec=_THANK_YOU_HANGUP_DELAY_SEC,
            )

    def _cancel_evaluate_task(self) -> None:
        if self._hangup_scheduled:
            return
        if self._evaluate_task and not self._evaluate_task.done():
            self._evaluate_task.cancel()
        self._evaluate_task = None

    def _should_disconnect(self, text: str) -> tuple[bool, str]:
        if self._ctx_ready_for_thank_you_hangup() and agent_said_thank_you(text):
            return True, "thank_you"
        final = self._ctx_final_confirm()
        if _should_hangup_on_agent_text(text, final_confirm_received=final):
            return True, "closing_phrase"
        if final and _should_force_hangup_after_final_confirm(
            text,
            agent_turns_after_final_confirm=self._ctx_agent_turns_after_confirm(),
        ):
            return True, "post_final_confirm"
        return False, ""

    async def _evaluate_hangup_after_stop(self, *, reason: str) -> None:
        try:
            await asyncio.sleep(_POST_STOP_TEXT_WAIT_SEC)
            text = self._best_agent_text()
            ok, why = self._should_disconnect(text)
            if ok:
                self._schedule_hangup(heard=text, reason=f"{reason}:{why}")
            else:
                logger.info(
                    "Outbound: no hangup after %s (final_confirm=%s turns=%s text=%r)",
                    reason,
                    self._ctx_final_confirm(),
                    self._ctx_agent_turns_after_confirm(),
                    text[:100],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Outbound: hangup evaluation failed")

    def _queue_hangup_evaluation(self, *, reason: str) -> None:
        if self._hangup_scheduled:
            return
        if self._evaluate_task and not self._evaluate_task.done():
            self._evaluate_task.cancel()
        self._evaluate_task = asyncio.create_task(
            self._evaluate_hangup_after_stop(reason=reason)
        )

    def _increment_agent_turn_after_confirm(self) -> None:
        if not self._ctx_final_confirm():
            return
        ctx = self._load_ctx()
        if not ctx:
            return
        from app.services.call_context_store import save_context

        ctx.agent_turns_after_final_confirm = (
            int(getattr(ctx, "agent_turns_after_final_confirm", 0) or 0) + 1
        )
        save_context(ctx)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._hangup_scheduled:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, BotStartedSpeakingFrame):
            if not self._ctx_final_confirm():
                self._cancel_evaluate_task()
                self._turn_text = ""
        elif isinstance(frame, (LLMTextFrame, TTSTextFrame, AggregatedTextFrame)):
            self._note_agent_text(getattr(frame, "text", None) or "")
            if self._hangup_scheduled:
                await self.push_frame(frame, direction)
                return
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._increment_agent_turn_after_confirm()
            self._queue_hangup_evaluation(reason="response_end")
        elif isinstance(frame, (BotStoppedSpeakingFrame, TTSStoppedFrame)):
            self._queue_hangup_evaluation(reason="bot_stopped")

        await self.push_frame(frame, direction)
