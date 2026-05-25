"""Voice pipelines: telephony and browser WebSocket."""

from __future__ import annotations

import logging
import re

from fastapi import WebSocket
from pipecat.runner.utils import (
    _create_telephony_transport,
    _get_transport_params,
    parse_telephony_websocket,
)
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.ai.audio_config import (
    BROWSER_AUDIO_PACKET_BYTES,
    BROWSER_INPUT_SAMPLE_RATE,
    GEMINI_OUTPUT_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
    outbound_telephony_vad_analyzer,
    twilio_vad_analyzer,
)
from app.ai.session import run_pipeline
from app.serializers.exotel_outbound import OutboundExotelFrameSerializer
from app.models.call_context import OutboundCallContext
from app.serializers.browser_audio import BrowserAudioSerializer
from app.services.call_context_store import get_context
from app.utils.config import Settings

logger = logging.getLogger(__name__)

_UUID_SEARCH = re.compile(
    "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def _uuid_in_string(text: object) -> str | None:
    """First UUID-like substring (outbound ``session_id`` format)."""
    t = str(text or "").strip()
    if not t:
        return None
    m = _UUID_SEARCH.search(t)
    return m.group(0) if m else None


def _telephony_params_factory(
    *, outbound: bool = False, outbound_browser_parity: bool = False
) -> FastAPIWebsocketParams:
    """
    Outbound PSTN with ``outbound_browser_parity``: same turn-taking stack as ``/browser/outbound``
    (Gemini VAD, interruptions) while keeping Exotel/Twilio 8 kHz media.
    """
    if outbound and outbound_browser_parity:
        return FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            vad_analyzer=None,
        )
    vad = outbound_telephony_vad_analyzer() if outbound else twilio_vad_analyzer()
    return FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=TWILIO_SAMPLE_RATE,
        audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        vad_analyzer=vad,
    )


def _telephony_transport_params(
    *, outbound: bool = False, outbound_browser_parity: bool = False
) -> dict:
    def factory() -> FastAPIWebsocketParams:
        return _telephony_params_factory(
            outbound=outbound, outbound_browser_parity=outbound_browser_parity
        )

    return {
        "twilio": factory,
        "telnyx": factory,
        "plivo": factory,
        "exotel": factory,
    }


def _resolve_outbound_context(session_id: str | None) -> OutboundCallContext | None:
    if not session_id:
        return None
    return get_context(str(session_id))


def _session_id_from_call_data(transport_type: str, call_data: dict) -> str | None:
    """Resolve outbound session_id from telephony start payload (Exotel drops WSS query params)."""
    if transport_type == "exotel":
        cp = call_data.get("custom_parameters")
        if isinstance(cp, dict):
            for key in (
                "session_id",
                "SessionId",
                "CustomField",
                "custom_field",
                "CUSTOMFIELD",
            ):
                sid = str(cp.get(key) or "").strip()
                if sid:
                    return sid
            for _k, v in cp.items():
                u = _uuid_in_string(v)
                if u:
                    return u
    elif transport_type == "twilio":
        body = call_data.get("body")
        if isinstance(body, dict):
            sid = str(body.get("session_id") or "").strip()
            if sid:
                return sid
    return None


def _apply_outbound_exotel_serializer(transport, *, transport_type: str) -> None:
    """Use serializer that does not send Exotel ``clear`` on interruption."""
    if transport_type != "exotel":
        return
    try:
        output = transport.output()
        params = getattr(output, "_params", None)
        serializer = getattr(params, "serializer", None) if params else None
        if serializer is None or not hasattr(serializer, "_stream_sid"):
            return
        params.serializer = OutboundExotelFrameSerializer(
            stream_sid=serializer._stream_sid,
            call_sid=getattr(serializer, "_call_sid", None),
            params=getattr(serializer, "_params", None),
        )
        logger.info("Exotel: using OutboundExotelFrameSerializer (no clear on interrupt)")
    except Exception:
        logger.exception("Failed to apply OutboundExotelFrameSerializer")


async def run_telephony_voice_session(
    websocket: WebSocket,
    settings: Settings,
    *,
    session_id: str | None = None,
) -> None:
    """Telephony media stream (Twilio / Exotel / Plivo) → Gemini Live."""
    session_id = (session_id or websocket.query_params.get("session_id") or "").strip() or None

    transport_type, call_data = await parse_telephony_websocket(websocket)
    if not session_id:
        session_id = _session_id_from_call_data(transport_type, call_data)
    if session_id:
        logger.info(
            "Telephony session_id=%s (transport=%s, from_query=%s)",
            session_id,
            transport_type,
            bool(websocket.query_params.get("session_id")),
        )

    outbound_ctx = _resolve_outbound_context(session_id)
    is_outbound = outbound_ctx is not None
    label = "outbound-confirm" if is_outbound else "inbound"

    params = _get_transport_params(
        transport_type,
        _telephony_transport_params(
            outbound=is_outbound,
            outbound_browser_parity=is_outbound,
        ),
    )
    transport = await _create_telephony_transport(
        websocket, params, transport_type, call_data
    )
    if transport_type == "exotel":
        _apply_outbound_exotel_serializer(transport, transport_type=transport_type)

    await run_pipeline(
        transport,
        settings,
        audio_in_sample_rate=TWILIO_SAMPLE_RATE,
        audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        label=label,
        # PSTN outbound: match ``run_browser_outbound_session`` (Gemini VAD, interruptions).
        # PSTN inbound: Silero turns + interruptions off (prior behavior).
        use_pipecat_vad=not is_outbound,
        greet_on_start=True,
        allow_interruptions=is_outbound,
        outbound_context=outbound_ctx,
        pstn_wire=True,
    )


async def run_twilio_voice_session(
    websocket: WebSocket,
    settings: Settings,
    *,
    session_id: str | None = None,
) -> None:
    """Backward-compatible alias for inbound/outbound telephony."""
    await run_telephony_voice_session(websocket, settings, session_id=session_id)


async def run_browser_outbound_session(
    websocket: WebSocket,
    settings: Settings,
    *,
    session_id: str,
) -> None:
    """Browser simulation of outbound order confirmation (same AI flow as phone)."""
    outbound_ctx = _resolve_outbound_context(session_id)
    if not outbound_ctx:
        raise ValueError(f"Invalid or expired session_id: {session_id}")

    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=BROWSER_INPUT_SAMPLE_RATE,
        audio_out_sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
        vad_analyzer=None,
        add_wav_header=False,
        fixed_audio_packet_size=BROWSER_AUDIO_PACKET_BYTES,
        serializer=BrowserAudioSerializer(sample_rate=GEMINI_OUTPUT_SAMPLE_RATE),
    )
    transport = FastAPIWebsocketTransport(websocket=websocket, params=params)
    logger.info(
        "Browser OUTBOUND session order=%s session_id=%s",
        outbound_ctx.order_id,
        session_id,
    )
    await run_pipeline(
        transport,
        settings,
        audio_in_sample_rate=BROWSER_INPUT_SAMPLE_RATE,
        audio_out_sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
        label="browser-outbound",
        use_pipecat_vad=False,
        greet_on_start=True,
        allow_interruptions=True,
        outbound_context=outbound_ctx,
    )


async def run_browser_voice_session(websocket: WebSocket, settings: Settings) -> None:
    """Browser mic (16 kHz PCM) → Gemini Live (24 kHz out) → gapless browser playback."""
    params = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=BROWSER_INPUT_SAMPLE_RATE,
        audio_out_sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
        # Gemini native VAD (not Silero) — detects when customer finishes speaking.
        vad_analyzer=None,
        add_wav_header=False,
        fixed_audio_packet_size=BROWSER_AUDIO_PACKET_BYTES,
        serializer=BrowserAudioSerializer(sample_rate=GEMINI_OUTPUT_SAMPLE_RATE),
    )
    transport = FastAPIWebsocketTransport(websocket=websocket, params=params)
    logger.info(
        "Browser voice session started (in=%s Hz, out=%s Hz, Gemini VAD)",
        BROWSER_INPUT_SAMPLE_RATE,
    )
    await run_pipeline(
        transport,
        settings,
        audio_in_sample_rate=BROWSER_INPUT_SAMPLE_RATE,
        audio_out_sample_rate=GEMINI_OUTPUT_SAMPLE_RATE,
        label="browser",
        use_pipecat_vad=False,
        greet_on_start=True,
        allow_interruptions=True,
    )
