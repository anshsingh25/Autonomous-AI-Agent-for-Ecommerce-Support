"""Hang up outbound telephony calls after the agent's closing statement."""

from __future__ import annotations

import asyncio
import logging
import re

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMTextFrame,
    TTSTextFrame,
)
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

# Match mandatory closing lines from prompts_outbound (EN + common HI variants).
_CLOSING_PHRASES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"thank you for your time",
        r"thank you so much",
        r"have a (?:wonderful|great|lovely|nice) day",
        r"order is confirmed.*delivery",
        r"processed for delivery soon",
        r"follow[- ]?up call shortly",
        r"cancellation request has been processed",
        r"confirm ho gaya",
        r"delivery ke liye confirm",
        r"dhanyavaad.*(?:din|samay|call)",
        r"shukriya.*(?:din|samay)",
    )
)

# Minimum spoken length so short "thank you" mid-call does not hang up.
_MIN_CLOSING_UTTERANCE_LEN = 40


def utterance_looks_like_call_closing(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) < _MIN_CLOSING_UTTERANCE_LEN:
        return False
    return any(p.search(cleaned) for p in _CLOSING_PHRASES)


class OutboundHangupOnCloseProcessor(FrameProcessor):
    """
    After the agent delivers a closing/goodbye line, end the call automatically.

    Watches each agent turn (LLM/TTS text + BotStoppedSpeaking) and schedules
    pipeline shutdown so Exotel disconnects once audio finishes playing.
    """

    def __init__(
        self,
        task_holder: list[PipelineTask | None],
        *,
        hangup_delay_sec: float = 2.5,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._task_holder = task_holder
        self._hangup_delay_sec = hangup_delay_sec
        self._turn_text = ""
        self._hangup_scheduled = False

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

    def _schedule_hangup(self, *, heard: str) -> None:
        if self._hangup_scheduled:
            return
        self._hangup_scheduled = True
        logger.info(
            "Outbound: closing detected, will hang up in %.1fs — %r",
            self._hangup_delay_sec,
            heard[:120],
        )
        asyncio.create_task(self._hangup_after_delay())

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM and not self._hangup_scheduled:
            if isinstance(frame, BotStartedSpeakingFrame):
                self._turn_text = ""
            elif isinstance(frame, (LLMTextFrame, TTSTextFrame)):
                chunk = getattr(frame, "text", None) or ""
                if chunk:
                    self._turn_text += chunk
            elif isinstance(frame, BotStoppedSpeakingFrame):
                if utterance_looks_like_call_closing(self._turn_text):
                    self._schedule_hangup(heard=self._turn_text.strip())

        await self.push_frame(frame, direction)
