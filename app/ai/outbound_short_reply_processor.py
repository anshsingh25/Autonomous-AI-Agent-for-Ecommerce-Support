"""Boost Gemini responses for short outbound replies (e.g. single-word "hindi")."""

from __future__ import annotations

import asyncio
import logging
import re
import time

from pipecat.frames.frames import (
    Frame,
    InputTextRawFrame,
    LLMMessagesAppendFrame,
    LLMRunFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

from app.ai.prompts_outbound import (
    ORDER_CONFIRMATION_OPENING_PURPOSE_EN,
    ORDER_CONFIRMATION_OPENING_PURPOSE_HI,
)

logger = logging.getLogger(__name__)

# Customer chose Hindi (one word or short phrase).
_HINDI_CHOICE = re.compile(
    r"(?:^|\b)(hindi|हिंदी|हिन्दी|haan\s+hindi|hindi\s+mein|hindi\s+me|"
    r"comfortable\s+(?:with\s+)?hindi|hindi\s+is\s+fine|i\s+prefer\s+hindi|"
    r"(?:i\s+am\s+)?(?:comfortable|fine|ok|okay)\s+(?:with\s+)?hindi)(?:\b|$)",
    re.IGNORECASE,
)

# Customer chose English.
_ENGLISH_CHOICE = re.compile(
    r"(?:^|\b)(english|angrezi|inglish|"
    r"comfortable\s+(?:with\s+)?english|english\s+is\s+fine|i\s+prefer\s+english|"
    r"(?:i\s+am\s+)?(?:comfortable|fine|ok|okay)\s+(?:with\s+)?english)(?:\b|$)",
    re.IGNORECASE,
)

# Single-word language pick — common when VAD cuts utterances short.
_SHORT_ONLY = re.compile(
    r"^\s*(hindi|english|angrezi|inglish|haan\s+hindi|हिंदी|हिन्दी)\s*[.!]?\s*$",
    re.IGNORECASE,
)

def _clean_asr_text(text: str) -> str:
    """Strip ASR bracket noise e.g. '[Hindi.]' -> 'Hindi'."""
    cleaned = (text or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        cleaned = cleaned[1:-1].strip()
    return cleaned.rstrip(".").strip()


def detect_language_choice(text: str) -> str | None:
    """Return 'hi', 'en', or None from transcript text."""
    cleaned = _clean_asr_text(text)
    if not cleaned:
        return None

    if _SHORT_ONLY.match(cleaned):
        low = cleaned.lower()
        if "hindi" in low or "हिं" in cleaned:
            return "hi"
        return "en"

    hi = bool(_HINDI_CHOICE.search(cleaned))
    en = bool(_ENGLISH_CHOICE.search(cleaned))
    if hi and not en:
        return "hi"
    if en and not hi:
        return "en"
    if hi and en:
        return "hi" if cleaned.lower().find("hindi") <= cleaned.lower().find("english") else "en"
    return None


class OutboundShortReplyProcessor(FrameProcessor):
    """
    When the customer picks a language (voice or browser button), force Gemini to respond.

    Voice-only "hindi" often never completes a Gemini transcription turn; we also accept
    InputTextRawFrame from the browser language buttons.
    """

    def __init__(
        self,
        llm: GeminiLiveLLMService,
        *,
        customer_name: str,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._llm = llm
        self._customer_name = customer_name or "there"
        self._last_nudge_at = 0.0
        self._nudge_in_flight = False
        self._language_locked = False
        self._started_at = time.monotonic()

    def _can_language_nudge(self, heard: str) -> bool:
        """
        Keep language nudges to the opening stage only.

        Without this gate, a later sentence containing "hindi/english" can
        accidentally trigger opening-purpose nudges and feel out of context.
        """
        if self._language_locked:
            return False
        # Only during early call window.
        if time.monotonic() - self._started_at > 45:
            return False
        # If utterance is long, likely regular conversation not language pick.
        if len((heard or "").split()) > 6:
            return False
        return True

    async def _nudge_language(self, lang: str, heard: str, *, via_text: bool) -> None:
        import time

        now = time.monotonic()
        if self._nudge_in_flight or now - self._last_nudge_at < 0.5:
            return

        self._nudge_in_flight = True
        self._last_nudge_at = now

        try:
            if lang == "hi":
                purpose = ORDER_CONFIRMATION_OPENING_PURPOSE_HI.format(
                    customer_name=self._customer_name
                )
                speak_word = "hindi"
                instruction = (
                    "[System — not the customer] The customer chose HINDI "
                    f'(they said: "{heard}"). Respond IMMEDIATELY in Hindi. '
                    f'Say ONLY this purpose line now, then listen: "{purpose}"'
                )
            else:
                purpose = ORDER_CONFIRMATION_OPENING_PURPOSE_EN.format(
                    customer_name=self._customer_name
                )
                speak_word = "english"
                instruction = (
                    "[System — not the customer] The customer chose ENGLISH "
                    f'(they said: "{heard}"). Respond IMMEDIATELY in English. '
                    f'Say ONLY this purpose line now, then listen: "{purpose}"'
                )

            logger.info(
                "Outbound language nudge (%s) heard=%r via_text=%s",
                lang,
                heard,
                via_text,
            )

            # Realtime text input — completes Gemini turn for short / language picks.
            if via_text or len(heard.split()) <= 4 or detect_language_choice(heard):
                await self._llm._send_user_text(speak_word)

            await self._llm.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": instruction}],
                ),
                FrameDirection.DOWNSTREAM,
            )
            await self._llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
            self._language_locked = True
        finally:
            self._nudge_in_flight = False

    def _schedule_language(self, lang: str, heard: str, *, via_text: bool) -> None:
        asyncio.create_task(self._nudge_language(lang, heard, via_text=via_text))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputTextRawFrame) and frame.text:
            lang = detect_language_choice(frame.text)
            if lang and self._can_language_nudge(frame.text.strip()):
                self._schedule_language(lang, frame.text.strip(), via_text=True)

        # Gemini STT → TranscriptionFrame (upstream from LLM).
        if (
            isinstance(frame, TranscriptionFrame)
            and frame.text
            and direction == FrameDirection.UPSTREAM
        ):
            lang = detect_language_choice(frame.text)
            if lang and self._can_language_nudge(frame.text.strip()):
                self._schedule_language(lang, frame.text.strip(), via_text=False)

        await self.push_frame(frame, direction)
