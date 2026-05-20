"""Exotel serializer that avoids ``clear`` on spurious interruptions (outbound)."""

from __future__ import annotations

import logging

from pipecat.frames.frames import Frame, InterruptionFrame
from pipecat.serializers.exotel import ExotelFrameSerializer

logger = logging.getLogger(__name__)


class OutboundExotelFrameSerializer(ExotelFrameSerializer):
    """
    Exotel ``clear`` flushes all queued audio — causes mid-sentence cutoffs.

    Outbound confirmation calls ignore InterruptionFrame for the wire protocol.
    """

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, InterruptionFrame):
            logger.debug("Outbound Exotel: skipping clear on interruption")
            return None
        return await super().serialize(frame)
