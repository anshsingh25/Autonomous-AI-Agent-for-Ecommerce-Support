"""Twilio voice webhook helpers."""

from app.utils.config import Settings


class TwilioService:
    """Build TwiML responses for Twilio Media Streams."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def incoming_call_twiml(self, websocket_url: str) -> str:
        """TwiML that connects the caller to our Pipecat WebSocket."""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{websocket_url}" />
  </Connect>
  <Pause length="40"/>
</Response>"""

    def resolve_websocket_url(self) -> str:
        """Public WebSocket URL Twilio will connect to."""
        url = self._settings.public_ws_url.strip()
        if not url:
            raise ValueError(
                "PUBLIC_WS_URL is not set. Example: wss://your-subdomain.ngrok-free.app/voice/ws"
            )
        if url.startswith("https://"):
            url = "wss://" + url[len("https://") :]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://") :]
        if not url.endswith("/voice/ws"):
            url = url.rstrip("/") + "/voice/ws"
        return url
