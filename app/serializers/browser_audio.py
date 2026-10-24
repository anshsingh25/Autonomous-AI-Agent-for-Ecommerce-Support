"""Simple PCM serializer for browser WebSocket clients."""

from __future__ import annotations

import json
from typing import Optional

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTextRawFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from app.ai.frames import InboundTypedOrderFrame
from app.services.inbound_session_store import set_typed_order_id


class BrowserAudioSerializer(FrameSerializer):
    """
    Browser protocol:
    - Client → server: binary PCM16 mono (little-endian)
    - Server → client: binary PCM16 (WAV header added by transport when add_wav_header=True)
    - Either direction may send UTF-8 JSON for control messages
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        num_channels: int = 1,
        *,
        inbound_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._inbound_session_id = (inbound_session_id or "").strip()

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            return json.dumps(frame.message)
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, str):
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                return None
            if message.get("type") == "ping":
                return None
            if message.get("type") == "language":
                lang = (message.get("lang") or "").lower()
                text = "hindi" if lang in ("hi", "hindi", "hin") else "english"
                return InputTextRawFrame(text=text)
            if message.get("type") == "order_id":
                raw = str(message.get("order_id") or "").strip()
                sid = str(message.get("session_id") or self._inbound_session_id).strip()
                if not raw or not sid:
                    return None
                cleaned = set_typed_order_id(sid, raw)
                if not cleaned:
                    return None
                return InboundTypedOrderFrame(session_id=sid, order_id=cleaned)
            return None

        if isinstance(data, (bytes, bytearray)) and len(data) > 0:
            return InputAudioRawFrame(
                audio=bytes(data),
                sample_rate=self._sample_rate,
                num_channels=self._num_channels,
            )
        return None
