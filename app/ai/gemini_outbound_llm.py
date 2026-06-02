"""Gemini Live — same as inbound except fast flush for complete short replies."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from pipecat.frames.frames import Frame, InterruptionFrame, TranscriptionFrame
from pipecat.utils.time import time_now_iso8601
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import (
    GeminiLiveLLMService,
    match_endofsentence,
)
from pipecat.services.google.gemini_live.llm import LiveServerMessage

from app.ai.outbound_short_reply_processor import detect_language_choice
from app.utils.config import get_settings

logger = logging.getLogger(__name__)

_INBOUND_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK = 0.038
_SHORT_REPLY_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK = 0.028
_ULTRA_SHORT_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK = 0.025
_LANGUAGE_PHRASE_MAX_LEN = 64

_SHORT_UTTERANCE = re.compile(
    r"^\s*(yes|no|yeah|yep|nahi|haan|han|ji|ok|okay|sure|fine|english|hindi|"
    r"angrezi|inglish|cancel|haan\s+ji|theek)\s*[.!]?\s*$",
    re.IGNORECASE,
)

_INSTANT_SHORT_TOKENS = frozenset(
    {
        "yes",
        "no",
        "yeah",
        "yep",
        "nahi",
        "haan",
        "han",
        "ji",
        "ok",
        "okay",
        "sure",
        "fine",
        "english",
        "hindi",
        "angrezi",
        "inglish",
        "cancel",
        "theek",
    }
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

# Early partial STT while the customer is still saying a one-word answer.
_SHORT_BUILDING_PREFIXES = (
    "y",
    "ye",
    "yes",
    "n",
    "no",
    "na",
    "nah",
    "hi",
    "hin",
    "hindi",
    "en",
    "eng",
    "engli",
    "english",
    "ha",
    "haa",
    "haan",
    "ok",
    "ji",
    "an",
    "ang",
    "the",
    "thee",
    "theek",
)


def _normalize_short_token(text: str) -> str:
    return re.sub(r"[^\w\s]", "", (text or "").strip().lower())


def _buffer_is_instant_short(buf: str) -> bool:
    """Complete one-word answers — flush immediately, no silence wait."""
    tok = _normalize_short_token(buf)
    if not tok:
        return False
    if tok in _INSTANT_SHORT_TOKENS:
        return True
    return bool(detect_language_choice(buf))


def _buffer_is_complete_short_reply(buf: str) -> bool:
    cleaned = buf.strip()
    if not cleaned:
        return False
    return bool(detect_language_choice(cleaned) or _SHORT_UTTERANCE.match(cleaned))


def _buffer_is_building_short_reply(buf: str) -> bool:
    cleaned = buf.strip().lower().strip(".,!?")
    if not cleaned or len(cleaned) > _LANGUAGE_PHRASE_MAX_LEN:
        return False
    if _buffer_is_complete_short_reply(buf) or _buffer_is_instant_short(buf):
        return True
    if detect_language_choice(buf):
        return True
    if cleaned in _SHORT_BUILDING_PREFIXES:
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
        cfg = get_settings()
        llm._outbound_transcription_min_interval = (
            cfg.gemini_outbound_transcription_debounce_sec
        )
        llm._outbound_flush_ultra_short_sec = (
            cfg.gemini_outbound_transcription_flush_ultra_short_sec
        )
        llm._outbound_flush_short_sec = cfg.gemini_outbound_transcription_flush_short_sec
        llm._outbound_flush_long_sec = cfg.gemini_outbound_transcription_flush_long_sec
        llm._transcription_timeout_handler = cls._transcription_timeout_handler.__get__(
            llm, GeminiLiveLLMService
        )
        llm._flush_short_buffer_if_ready = cls._flush_short_buffer_if_ready.__get__(
            llm, GeminiLiveLLMService
        )
        llm._handle_msg_input_transcription = cls._handle_msg_input_transcription.__get__(
            llm, GeminiLiveLLMService
        )
        llm._push_user_transcription = cls._push_user_transcription_finalized.__get__(
            llm, GeminiLiveLLMService
        )
        llm._push_user_transcription_debounced = (
            cls._push_user_transcription_debounced.__get__(llm, GeminiLiveLLMService)
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
        llm._push_output_transcription_text_frames = (
            cls._push_output_transcription_with_context.__get__(llm, GeminiLiveLLMService)
        )

    async def _push_output_transcription_with_context(self, text: str) -> None:
        """Mirror agent speech into call context for reliable PSTN hangup detection."""
        sid = getattr(self, "_outbound_session_id", None)
        chunk = (text or "").strip()
        if sid and chunk:
            from app.services.call_context_store import get_context, save_context

            ctx = get_context(str(sid))
            if ctx:
                ctx.recent_agent_speech = (ctx.recent_agent_speech + chunk)[-1200:]
                save_context(ctx)
        await GeminiLiveLLMService._push_output_transcription_text_frames(self, text)

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

    async def _transcription_timeout_handler(self) -> None:
        try:
            buf = self._user_transcription_buffer.strip()
            ultra_d = float(
                getattr(
                    self,
                    "_outbound_flush_ultra_short_sec",
                    _ULTRA_SHORT_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK,
                )
            )
            short_d = float(
                getattr(
                    self,
                    "_outbound_flush_short_sec",
                    _SHORT_REPLY_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK,
                )
            )
            long_d = float(
                getattr(
                    self,
                    "_outbound_flush_long_sec",
                    _INBOUND_TRANSCRIPTION_TIMEOUT_SEC_FALLBACK,
                )
            )
            if _buffer_is_instant_short(buf) or _buffer_is_complete_short_reply(buf):
                delay = ultra_d
            elif _buffer_is_building_short_reply(buf):
                delay = short_d
            else:
                delay = long_d
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

    async def _push_user_transcription_finalized(
        self, text: str, result: Optional[LiveServerMessage] = None
    ) -> None:
        """Push finalized user STT so Pipecat ends the turn without Smart Turn delay."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        await self._push_user_transcription_debounced(cleaned, result)

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
        min_iv = float(
            getattr(self, "_outbound_transcription_min_interval", 0.03)
        )
        is_short = _buffer_is_instant_short(cleaned) or detect_language_choice(cleaned)
        if now - last_ts < min_iv:
            if is_short and cleaned.lower() != last_text.lower():
                pass
            else:
                logger.debug("[Transcription:user:debounced] [%s]", cleaned)
                return
        self._outbound_last_push_mono = now
        self._outbound_last_push_text = cleaned
        await GeminiLiveLLMService._handle_user_transcription(
            self, cleaned, True, self._settings.language
        )
        await self.push_frame(
            TranscriptionFrame(
                text=cleaned,
                user_id="",
                timestamp=time_now_iso8601(),
                result=result,
                finalized=True,
            ),
            FrameDirection.UPSTREAM,
        )

    async def _flush_short_buffer_if_ready(
        self, message: Optional[LiveServerMessage]
    ) -> bool:
        buf = self._user_transcription_buffer.strip()
        if not buf:
            return False
        if not (
            _buffer_is_instant_short(buf)
            or _buffer_is_complete_short_reply(buf)
        ):
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
