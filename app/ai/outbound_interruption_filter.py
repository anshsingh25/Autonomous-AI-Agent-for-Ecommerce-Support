"""Drop interruption frames before telephony output on outbound calls."""

from __future__ import annotations

import logging

from pipecat.frames.frames import CancelFrame, EndFrame, Frame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class OutboundInterruptionFilter(FrameProcessor):
    """Prevent Exotel ``clear`` and TTS stop from spurious barge-in signals."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            logger.debug(
                "Outbound: dropped InterruptionFrame (direction=%s)", direction
            )
            return

        await self.push_frame(frame, direction)

        if isinstance(frame, (EndFrame, CancelFrame)):
            return
