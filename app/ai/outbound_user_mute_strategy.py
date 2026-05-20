"""Mute customer mic for full agent turns (not brief TTS packet gaps)."""

from __future__ import annotations

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    BotStartedSpeakingFrame,
    Frame,
    TTSStoppedFrame,
)
from pipecat.turns.user_mute.base_user_mute_strategy import BaseUserMuteStrategy


class OutboundAgentTurnMuteStrategy(BaseUserMuteStrategy):
    """
    Keep the customer muted until the agent turn ends.

    Prefer TTSStoppedFrame, but also accept BotStoppedSpeakingFrame fallback.
    Some pipelines do not propagate TTSStoppedFrame to user-mute strategies;
    without fallback, customer stays muted for the whole call.
    """

    def __init__(self) -> None:
        super().__init__()
        self._agent_speaking = False

    async def reset(self) -> None:
        await super().reset()
        self._agent_speaking = False

    async def process_frame(self, frame: Frame) -> bool:
        await super().process_frame(frame)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._agent_speaking = True
        elif isinstance(frame, (TTSStoppedFrame, BotStoppedSpeakingFrame)):
            self._agent_speaking = False

        return self._agent_speaking
