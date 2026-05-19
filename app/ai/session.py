"""Shared Pipecat session builder for Twilio and browser transports."""

from __future__ import annotations

import asyncio
import logging

from pipecat.frames.frames import LLMMessagesAppendFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, GeminiVADParams
from pipecat.transports.base_transport import BaseTransport

from app.ai.audio_config import (
    BROWSER_INPUT_SAMPLE_RATE,
    GEMINI_OUTPUT_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
)
from app.ai.prompts import OPENING_GREETING, SYSTEM_INSTRUCTION
from app.ai.tools import build_tools_schema, register_ecommerce_tools
from app.utils.config import Settings

logger = logging.getLogger(__name__)

# Backward-compatible alias (input rate for browser mic).
BROWSER_SAMPLE_RATE = BROWSER_INPUT_SAMPLE_RATE


def create_gemini_llm(
    settings: Settings,
    *,
    use_pipecat_vad: bool = False,
) -> GeminiLiveLLMService:
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is required")

    tools = build_tools_schema()
    gemini_settings = GeminiLiveLLMService.Settings(
        model=settings.gemini_model,
        voice=settings.gemini_voice,
        temperature=settings.gemini_temperature,
        max_tokens=settings.gemini_max_tokens,
    )
    # Pipecat Silero drives activity_start/end; disable duplicate Gemini VAD.
    if use_pipecat_vad:
        gemini_settings.vad = GeminiVADParams(disabled=True)
    else:
        # Gemini native VAD — better end-of-turn when customer speaks (browser).
        gemini_settings.vad = GeminiVADParams(
            disabled=False,
            prefix_padding_ms=300,
            silence_duration_ms=800,
        )

    llm = GeminiLiveLLMService(
        api_key=settings.gemini_api_key,
        system_instruction=SYSTEM_INSTRUCTION,
        tools=tools,
        settings=gemini_settings,
        # Avoid an extra model turn on connect (reduces talking over the customer).
        inference_on_context_initialization=False,
    )
    register_ecommerce_tools(llm)
    return llm


async def run_pipeline(
    transport: BaseTransport,
    settings: Settings,
    *,
    audio_in_sample_rate: int,
    audio_out_sample_rate: int | None = None,
    label: str = "session",
    use_pipecat_vad: bool = True,
    greet_on_start: bool = False,
    allow_interruptions: bool = True,
) -> None:
    """Run Gemini Live voice pipeline on any Pipecat transport."""
    out_rate = audio_out_sample_rate or audio_in_sample_rate
    llm = create_gemini_llm(settings, use_pipecat_vad=use_pipecat_vad)
    # Tools are registered on the LLM at init; do not duplicate them in context
    # or Gemini Live will disconnect/reconnect and drop the opening greeting.
    context = LLMContext()
    context_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            context_aggregator.user(),
            llm,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=out_rate,
            allow_interruptions=allow_interruptions,
        ),
        # Browser path often has no UserSpeakingFrame; don't auto-cancel mid-call.
        cancel_on_idle_timeout=False,
        idle_timeout_secs=None,
    )

    async def _wait_for_gemini_ready() -> bool:
        """Gemini Live drops audio until LLMContextFrame is handled."""
        await asyncio.sleep(0.2)
        await task.queue_frame(LLMRunFrame())

        stable_ticks = 0
        for _ in range(150):
            await asyncio.sleep(0.1)
            ready = (
                getattr(llm, "_ready_for_realtime_input", False)
                and getattr(llm, "_session", None)
                and not getattr(llm, "_disconnecting", False)
            )
            if ready:
                stable_ticks += 1
                if stable_ticks >= 3:
                    return True
            else:
                stable_ticks = 0
        return False

    async def _bootstrap_gemini() -> None:
        if not await _wait_for_gemini_ready():
            logger.warning("%s: timed out waiting for Gemini to accept input", label)
            return

        logger.info("%s: Gemini ready for voice", label)

        if not greet_on_start:
            return

        # send_client_content (not realtime text) — required for spoken AUDIO modality
        # when Pipecat VAD is driving activity_start/activity_end on Gemini.
        logger.info("%s: requesting welcome greeting", label)
        await llm.queue_frame(
            LLMMessagesAppendFrame(
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "[System — not the customer] A new voice call connected. "
                            f'Say ONLY this greeting verbatim, then stop and listen: '
                            f'"{OPENING_GREETING}" '
                            "Then wait for the customer. When they speak (even just hello), "
                            "reply to them naturally — never use silence-check phrases like "
                            "'Are you there?' if they just talked. "
                            "Do NOT ask for order number in the greeting. "
                            "Do NOT call tools until they give an order number."
                        ),
                    }
                ],
            ),
            FrameDirection.DOWNSTREAM,
        )
        await llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)

    async def _restore_ready_after_reconnect() -> None:
        """Gemini sets _ready_for_realtime_input=False on disconnect; restore after reconnect."""
        while True:
            await asyncio.sleep(1.0)
            if getattr(llm, "_disconnecting", False):
                continue
            session = getattr(llm, "_session", None)
            if not session or getattr(llm, "_ready_for_realtime_input", False):
                continue
            context = getattr(llm, "_context", None)
            if not context:
                continue
            try:
                adapter = llm.get_llm_adapter()
                messages = adapter.get_llm_invocation_params(context).get("messages", [])
                if messages:
                    # Mid-call reconnect: do not re-send full history (can drop the call).
                    llm._ready_for_realtime_input = True
                    logger.info("%s: restored realtime input after reconnect (mid-call)", label)
                else:
                    await llm._create_initial_response()
                    logger.info("%s: restored Gemini session after reconnect", label)
            except Exception:
                logger.exception("%s: failed to restore Gemini input", label)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport: BaseTransport, websocket) -> None:
        logger.info("%s: browser/client websocket disconnected", label)

    @task.event_handler("on_pipeline_started")
    async def on_started(task: PipelineTask, frame) -> None:
        asyncio.create_task(_bootstrap_gemini())
        asyncio.create_task(_restore_ready_after_reconnect())

    @task.event_handler("on_pipeline_error")
    async def on_pipeline_error(task: PipelineTask, frame) -> None:
        logger.error(
            "%s pipeline error (fatal=%s): %s",
            label,
            getattr(frame, "fatal", False),
            getattr(frame, "error", frame),
        )

    @task.event_handler("on_pipeline_finished")
    async def on_finished(task: PipelineTask, frame) -> None:
        logger.info("%s pipeline finished: %s", label, type(frame).__name__)

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
