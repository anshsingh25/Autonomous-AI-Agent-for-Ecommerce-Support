"""Gemini Live — same as inbound except fast flush for complete short replies."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from pipecat.frames.frames import Frame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    match_endofsentence,
)
from pipecat.services.google.gemini_live.llm import LiveServerMessage

from app.ai.outbound_short_reply_processor import detect_language_choice

logger = logging.getLogger(__name__)

_INBOUND_TRANSCRIPTION_TIMEOUT_SEC = 0.3
_SHORT_REPLY_TRANSCRIPTION_TIMEOUT_SEC = 0.12
_LANGUAGE_PHRASE_MAX_LEN = 64

_SHORT_UTTERANCE = re.compile(
    r"^\s*(yes|no|yeah|yep|nahi|haan|han|ji|ok|okay|sure|fine|english|hindi|"
    r"angrezi|inglish|cancel|haan\s+ji|theek)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_SHORT_REPLY_BUILDING = (
    "hindi",
    "english",
    "angrezi",
    "inglish",
    "haan",
    "nahi",
    "yes",
    "no",
    "cancel",
    "theek",
    "okay",
    "comfortable",
    "prefer",
    "language",
)


def _buffer_is_complete_short_reply(buf: str) -> bool:
    cleaned = buf.strip()
    if not cleaned:
        return False
    return bool(detect_language_choice(cleaned) or _SHORT_UTTERANCE.match(cleaned))


def _buffer_is_building_short_reply(buf: str) -> bool:
    cleaned = buf.strip().lower()
    if not cleaned or len(cleaned) > _LANGUAGE_PHRASE_MAX_LEN:
        return False
    if _buffer_is_complete_short_reply(buf):
        return True
    if detect_language_choice(buf):
        return True
    return any(word.startswith(cleaned) for word in _SHORT_REPLY_BUILDING)


class OutboundGeminiLiveLLMService(GeminiLiveLLMService):
    """
    Inbound uses GeminiLiveLLMService directly; outbound patches the same class
    so audio/TTS path is identical — only input transcription differs.
    """

    @classmethod
    def patch_short_reply_listening(cls, llm: GeminiLiveLLMService) -> None:
        """Apply short-word listening on a standard inbound LLM instance."""
        llm._outbound_last_push_mono = 0.0
        llm._outbound_last_push_text = ""
        llm._transcription_timeout_handler = cls._transcription_timeout_handler.__get__(
            llm, GeminiLiveLLMService
        )
        llm._flush_short_buffer_if_ready = cls._flush_short_buffer_if_ready.__get__(
            llm, GeminiLiveLLMService
        )
        llm._handle_msg_input_transcription = cls._handle_msg_input_transcription.__get__(
            llm, GeminiLiveLLMService
        )
        llm._push_user_transcription = cls._push_user_transcription_debounced.__get__(
            llm, GeminiLiveLLMService
        )

    @classmethod
    def patch_outbound_telephony_stability(cls, llm: GeminiLiveLLMService) -> None:
        """
        Exotel only: keep default Gemini connection loop, ignore interruption frames.

        The default upstream connection handler has better long-call stability;
        we only need interruption suppression at frame-processing level.
        """
        llm._outbound_telephony = True
        llm._connect = cls._connect_outbound_no_resumption.__get__(llm, GeminiLiveLLMService)
        llm.process_frame = cls._process_frame_outbound.__get__(llm, GeminiLiveLLMService)


    async def _connect_outbound_no_resumption(
        self, session_resumption_handle: str | None = None
    ) -> None:
        """Disable session resumption handle for outbound telephony stability."""
        if session_resumption_handle:
            logger.info("Outbound: ignoring session_resumption_handle to avoid 1008 reconnect errors")
        await GeminiLiveLLMService._connect(self, session_resumption_handle=None)

    async def _process_frame_outbound(
        self, frame: Frame, direction: FrameDirection
    ) -> None:
        """Ignore pipeline InterruptionFrame on outbound (avoids TTSStopped + Exotel clear)."""
        if isinstance(frame, InterruptionFrame) and getattr(
            self, "_outbound_telephony", False
        ):
            logger.debug("Outbound: ignoring InterruptionFrame in Gemini service")
            return
        await GeminiLiveLLMService.process_frame(self, frame, direction)

    async def _push_user_transcription_debounced(
        self, text: str, result: Optional[LiveServerMessage] = None
    ) -> None:
        """Debounce partial STT fragments; always allow a new complete short reply."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        now = time.monotonic()
        last_ts = getattr(self, "_outbound_last_push_mono", 0.0)
        last_text = getattr(self, "_outbound_last_push_text", "")
        if now - last_ts < 0.2:
            if (
                _buffer_is_complete_short_reply(cleaned)
                or detect_language_choice(cleaned)
            ) and cleaned.lower() != last_text.lower():
                pass
            else:
                logger.debug("[Transcription:user:debounced] [%s]", cleaned)
                return
        self._outbound_last_push_mono = now
        self._outbound_last_push_text = cleaned
        await GeminiLiveLLMService._push_user_transcription(self, cleaned, result)

    async def _transcription_timeout_handler(self) -> None:
        try:
            buf = self._user_transcription_buffer.strip()
            if _buffer_is_complete_short_reply(buf):
                delay = _SHORT_REPLY_TRANSCRIPTION_TIMEOUT_SEC
            elif _buffer_is_building_short_reply(buf):
                delay = _SHORT_REPLY_TRANSCRIPTION_TIMEOUT_SEC
            else:
                delay = _INBOUND_TRANSCRIPTION_TIMEOUT_SEC
            await asyncio.sleep(delay)
            if self._user_transcription_buffer:
                pending = self._user_transcription_buffer.strip()
                self._user_transcription_buffer = ""
                if not pending:
                    return
                logger.debug("[Transcription:user:timeout] [%s]", pending)
                await self._push_user_transcription(pending, result=None)
        except asyncio.CancelledError:
            raise

    async def _flush_short_buffer_if_ready(
        self, message: Optional[LiveServerMessage]
    ) -> bool:
        buf = self._user_transcription_buffer.strip()
        if not buf:
            return False
        if not _buffer_is_complete_short_reply(buf):
            return False
        if self._transcription_timeout_task:
            await self.cancel_task(self._transcription_timeout_task)
            self._transcription_timeout_task = None
        self._user_transcription_buffer = ""
        logger.debug("[Transcription:user:fast-flush] [%s]", buf)
        await self._push_user_transcription(buf, result=message)
        return True

    async def _handle_msg_input_transcription(self, message: LiveServerMessage) -> None:
        if not message.server_content.input_transcription:
            return

        text = message.server_content.input_transcription.text
        if not text:
            return

        if self._transcription_timeout_task:
            await self.cancel_task(self._transcription_timeout_task)
            self._transcription_timeout_task = None

        if text.startswith(" ") and not self._user_transcription_buffer:
            text = text.lstrip()

        self._user_transcription_buffer += text

        while True:
            eos_end_marker = match_endofsentence(self._user_transcription_buffer)
            if not eos_end_marker:
                break
            complete_sentence = self._user_transcription_buffer[:eos_end_marker]
            self._user_transcription_buffer = self._user_transcription_buffer[
                eos_end_marker:
            ]
            await self._push_user_transcription(complete_sentence, result=message)

        if await self._flush_short_buffer_if_ready(message):
            return

        if self._user_transcription_buffer:
            self._transcription_timeout_task = self.create_task(
                self._transcription_timeout_handler()
            )
            await asyncio.sleep(0)
