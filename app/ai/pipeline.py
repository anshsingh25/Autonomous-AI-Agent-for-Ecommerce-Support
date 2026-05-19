"""Voice pipelines: Twilio telephony and browser WebSocket."""

from __future__ import annotations

import logging

from fastapi import WebSocket
from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from app.ai.audio_config import (
    BROWSER_AUDIO_PACKET_BYTES,
    BROWSER_INPUT_SAMPLE_RATE,
    GEMINI_OUTPUT_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
    twilio_vad_analyzer,
)
from app.ai.session import run_pipeline
from app.serializers.browser_audio import BrowserAudioSerializer
from app.utils.config import Settings

logger = logging.getLogger(__name__)


def _twilio_transport_params() -> dict:
    return {
        "twilio": lambda: FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            vad_analyzer=twilio_vad_analyzer(),
        ),
    }


async def run_twilio_voice_session(websocket: WebSocket, settings: Settings) -> None:
    """Twilio Media Stream → Gemini Live → Twilio."""
    runner_args = WebSocketRunnerArguments(websocket=websocket)
    transport = await create_transport(runner_args, _twilio_transport_params())
    await run_pipeline(
        transport,
        settings,
        audio_in_sample_rate=TWILIO_SAMPLE_RATE,
        audio_out_sample_rate=TWILIO_SAMPLE_RATE,
        label="twilio",
        use_pipecat_vad=True,
        allow_interruptions=True,
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
