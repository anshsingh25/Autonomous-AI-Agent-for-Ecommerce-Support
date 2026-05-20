"""TwiML / voice markup for connecting media streams to our WebSocket."""

from __future__ import annotations

from urllib.parse import urlencode

from app.utils.config import Settings, get_settings


def _ws_base(settings: Settings) -> str:
    url = settings.public_ws_url.strip()
    if not url:
        raise ValueError("PUBLIC_WS_URL is not set")
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return url.rstrip("/")


def twilio_stream_twiml(session_id: str | None = None, *, settings: Settings | None = None) -> str:
    """TwiML that connects the call to our Pipecat WebSocket."""
    s = settings or get_settings()
    ws_url = _ws_base(s).rstrip("/") + "/voice/ws"
    if session_id:
        qs = urlencode({"session_id": session_id})
        ws_url = f"{ws_url}?{qs}"
    stream_inner = f'\n    <Stream url="{ws_url}" />'
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>{stream_inner}
  </Connect>
  <Pause length="40"/>
</Response>"""


def exotel_voicebot_hint(session_id: str, *, settings: Settings | None = None) -> dict:
    """Exotel voicebot apps use a static WSS URL; pass session_id as query param."""
    s = settings or get_settings()
    ws_url = _ws_base(s).rstrip("/") + "/voice/ws"
    qs = urlencode({"session_id": session_id, "sample-rate": "8000"})
    return {
        "websocket_url": f"{ws_url}?{qs}",
        "note": (
            "For Exotel App Bazaar: set Voicebot applet WebSocket URL to websocket_url, "
            "or use Calls/connect Url pointing to /voice/outbound/exotel-answer"
        ),
    }
