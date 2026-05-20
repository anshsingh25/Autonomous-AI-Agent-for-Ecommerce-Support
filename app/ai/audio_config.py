"""Audio sample rates and VAD tuning for voice quality."""

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

# Gemini Live native audio output is 24 kHz — do not downsample for browser playback.
GEMINI_OUTPUT_SAMPLE_RATE = 24000

# Silero VAD supports 8 kHz and 16 kHz only (mic / telephony input).
BROWSER_INPUT_SAMPLE_RATE = 16000
TWILIO_SAMPLE_RATE = 8000

# 20 ms PCM16 mono frames at 24 kHz (smooth WebSocket streaming to browser).
BROWSER_AUDIO_PACKET_BYTES = int(GEMINI_OUTPUT_SAMPLE_RATE * 0.02 * 2)


def browser_vad_analyzer() -> SileroVADAnalyzer:
    """Silero VAD for Twilio; browser uses Gemini native VAD instead."""
    return SileroVADAnalyzer(
        sample_rate=BROWSER_INPUT_SAMPLE_RATE,
        params=VADParams(
            confidence=0.6,
            start_secs=0.1,
            stop_secs=0.4,
            min_volume=0.25,
        ),
    )


def twilio_vad_analyzer() -> SileroVADAnalyzer:
    """8 kHz telephony — slightly shorter stop for phone latency."""
    return SileroVADAnalyzer(
        sample_rate=TWILIO_SAMPLE_RATE,
        params=VADParams(
            confidence=0.7,
            start_secs=0.15,
            stop_secs=0.35,
            min_volume=0.4,
        ),
    )


def outbound_telephony_vad_analyzer() -> SileroVADAnalyzer:
    """
    Outbound 8 kHz — balanced VAD for faster customer-turn pickup on PSTN calls.

    We still protect bot playback via OutboundAgentTurnMuteStrategy +
    OutboundInterruptionFilter, so VAD can be less strict than before.
    """
    return SileroVADAnalyzer(
        sample_rate=TWILIO_SAMPLE_RATE,
        params=VADParams(
            confidence=0.7,
            start_secs=0.15,
            stop_secs=0.35,
            min_volume=0.35,
        ),
    )
