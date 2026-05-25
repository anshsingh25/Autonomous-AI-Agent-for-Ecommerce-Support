"""Shared Pipecat session builder for Twilio and browser transports."""

from __future__ import annotations

import asyncio
import logging

from pipecat.frames.frames import LLMMessagesAppendFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_start.transcription_user_turn_start_strategy import (
    TranscriptionUserTurnStartStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService, GeminiVADParams

from app.ai.gemini_outbound_llm import OutboundGeminiLiveLLMService
from app.ai.outbound_call_state import inject_llm_reminder
from app.ai.outbound_hangup_processor import OutboundHangupOnCloseProcessor
from app.ai.outbound_interruption_filter import OutboundInterruptionFilter
from app.ai.outbound_mid_call_processor import OutboundMidCallProcessor
from app.ai.outbound_short_reply_processor import OutboundShortReplyProcessor
from app.ai.outbound_user_mute_strategy import OutboundAgentTurnMuteStrategy
from pipecat.transports.base_transport import BaseTransport

from app.ai.audio_config import (
    BROWSER_INPUT_SAMPLE_RATE,
    GEMINI_OUTPUT_SAMPLE_RATE,
    TWILIO_SAMPLE_RATE,
)
from app.ai.confirmation_outcome_processor import ConfirmationOutcomeProcessor
from app.ai.prompts import OPENING_GREETING, SYSTEM_INSTRUCTION
from app.ai.prompts_outbound import OUTBOUND_SYSTEM_INSTRUCTION, outbound_bootstrap_message
from app.ai.outbound_tools import (
    build_outbound_tools_schema,
    register_outbound_confirmation_tools,
)
from app.ai.tools import build_tools_schema, register_ecommerce_tools
from app.models.call_context import OutboundCallContext
from app.utils.config import Settings

logger = logging.getLogger(__name__)

# Backward-compatible alias (input rate for browser mic).
BROWSER_SAMPLE_RATE = BROWSER_INPUT_SAMPLE_RATE


def create_gemini_llm(
    settings: Settings,
    *,
    use_pipecat_vad: bool = False,
    system_instruction: str | None = None,
    enable_tools: bool = True,
    outbound_mode: bool = False,
    outbound_session_id: str | None = None,
) -> GeminiLiveLLMService:
    if not settings.gemini_api_key:
        raise ValueError("GEMINI_API_KEY is required")

    if outbound_session_id:
        tools = build_outbound_tools_schema()
    elif enable_tools:
        tools = build_tools_schema()
    else:
        tools = None
    # Don't cap outbound below config: long bilingual order recaps + full addresses hit 600
    # and truncate mid-speech — use GEMINI_MAX_TOKENS / temperature for pacing instead.
    max_tokens = settings.gemini_max_tokens
    gemini_settings = GeminiLiveLLMService.Settings(
        model=settings.gemini_model,
        voice=settings.gemini_voice,
        temperature=settings.gemini_temperature,
        max_tokens=max_tokens,
    )
    if use_pipecat_vad:
        gemini_settings.vad = GeminiVADParams(disabled=True)
    elif outbound_mode:
        # Snappier defaults for order-confirmation; tune with GEMINI_OUTBOUND_VAD_* in .env if needed.
        gemini_settings.vad = GeminiVADParams(
            disabled=False,
            prefix_padding_ms=settings.gemini_outbound_vad_prefix_padding_ms,
            silence_duration_ms=settings.gemini_outbound_vad_silence_duration_ms,
        )
    else:
        gemini_settings.vad = GeminiVADParams(
            disabled=False,
            prefix_padding_ms=150,
            silence_duration_ms=500,
        )

    llm = GeminiLiveLLMService(
        api_key=settings.gemini_api_key,
        system_instruction=system_instruction or SYSTEM_INSTRUCTION,
        tools=tools,
        settings=gemini_settings,
        inference_on_context_initialization=False,
    )
    if outbound_mode or use_pipecat_vad:
        OutboundGeminiLiveLLMService.patch_short_reply_listening(llm)
        # Stable Exotel/Media Stream + ignore pipeline InterruptionFrame (also needed when
        # outbound uses Gemini VAD, not only Pipecat Silero legs).
        OutboundGeminiLiveLLMService.patch_outbound_telephony_stability(llm)
    if outbound_session_id:
        register_outbound_confirmation_tools(llm, outbound_session_id)
    elif enable_tools:
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
    outbound_context: OutboundCallContext | None = None,
    pstn_wire: bool = False,
) -> None:
    """Run Gemini Live voice pipeline on any Pipecat transport."""
    out_rate = audio_out_sample_rate or audio_in_sample_rate
    is_outbound = outbound_context is not None
    # Pipecat Silero turns + disabled Gemini VAD — used on PSTN *inbound* and legacy PSTN outbound.
    is_pipecat_vad_mode = use_pipecat_vad
    outbound_instruction = OUTBOUND_SYSTEM_INSTRUCTION if is_outbound else None
    llm = create_gemini_llm(
        settings,
        use_pipecat_vad=use_pipecat_vad,
        system_instruction=outbound_instruction,
        enable_tools=not is_outbound,
        outbound_mode=is_outbound,
        outbound_session_id=(
            outbound_context.session_id if outbound_context else None
        ),
    )
    # Track whether we've already delivered the one-time opening greeting.
    llm._opening_sent = False
    context = LLMContext()
    user_params = None
    if is_pipecat_vad_mode:
        user_params = LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=False),
                    TranscriptionUserTurnStartStrategy(
                        use_interim=True, enable_interruptions=False
                    ),
                ],
            ),
            user_mute_strategies=[OutboundAgentTurnMuteStrategy()],
        )
    elif is_outbound:
        # Outbound uses Gemini-side VAD (no transport Silero). If user_params is
        # omitted, Pipecat defaults to transcription/VAD strategies with
        # enable_interruptions=True, which broadcasts InterruptionFrame on STT
        # noise/echo and cuts the agent. Match pipecat-vad telephony: suppress
        # Pipecat interruptions and mute user STT during agent turns (Gemini
        # still handles realtime barge-in on the Live session).
        user_params = LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[
                    VADUserTurnStartStrategy(enable_interruptions=False),
                    TranscriptionUserTurnStartStrategy(
                        use_interim=True, enable_interruptions=False
                    ),
                ],
            ),
            user_mute_strategies=[OutboundAgentTurnMuteStrategy()],
        )
    context_aggregator = LLMContextAggregatorPair(context, user_params=user_params)

    processors = [
        transport.input(),
        context_aggregator.user(),
    ]
    if is_outbound and outbound_context:
        processors.append(
            ConfirmationOutcomeProcessor(session_id=outbound_context.session_id)
        )
        processors.append(
            OutboundMidCallProcessor(
                llm,
                session_id=outbound_context.session_id,
            )
        )
    output_chain: list = [llm]
    if is_outbound and outbound_context:
        output_chain.append(
            OutboundShortReplyProcessor(
                llm,
                customer_name=outbound_context.customer_name,
                session_id=outbound_context.session_id,
            )
        )
    if is_pipecat_vad_mode:
        output_chain.append(OutboundInterruptionFilter())

    task_holder: list[PipelineTask | None] = [None]
    if is_outbound and outbound_context:
        output_chain.append(
            OutboundHangupOnCloseProcessor(
                task_holder,
                session_id=outbound_context.session_id,
                hangup_delay_sec=1.8,
            )
        )

    output_chain.extend([transport.output(), context_aggregator.assistant()])
    processors.extend(output_chain)

    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=out_rate,
            allow_interruptions=allow_interruptions,
        ),
        cancel_on_idle_timeout=False,
        idle_timeout_secs=None,
    )
    task_holder[0] = task

    async def _wait_for_gemini_ready() -> bool:
        """Gemini Live drops audio until LLMContextFrame is handled."""
        if is_pipecat_vad_mode:
            boot_sleep = 0.2
        elif is_outbound:
            boot_sleep = 0.12  # Faster opening on outbound Gemini-VAD legs
        else:
            boot_sleep = 0.22
        await asyncio.sleep(boot_sleep)
        poll_interval = 0.1
        max_ticks = 150
        # Outbound + Gemini VAD: require fewer stable polls so first greeting starts sooner.
        stable_required = 2 if is_outbound and not is_pipecat_vad_mode else 3

        await task.queue_frame(LLMRunFrame())

        stable_ticks = 0
        for _ in range(max_ticks):
            await asyncio.sleep(poll_interval)
            ready = (
                getattr(llm, "_ready_for_realtime_input", False)
                and getattr(llm, "_session", None)
                and not getattr(llm, "_disconnecting", False)
            )
            if ready:
                stable_ticks += 1
                if stable_ticks >= stable_required:
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
        if getattr(llm, "_opening_sent", False):
            return

        if outbound_context:
            bootstrap = outbound_bootstrap_message(
                outbound_context.summary_for_prompt(),
                outbound_context.customer_name,
            )
        else:
            bootstrap = (
                "[System — not the customer] A new voice call connected. "
                f'Say ONLY this greeting verbatim, then stop and listen: '
                f'"{OPENING_GREETING}" '
                "Then wait for the customer. When they speak (even just hello), "
                "reply to them naturally — never use silence-check phrases like "
                "'Are you there?' if they just talked. "
                "Do NOT ask for order number in the greeting. "
                "Do NOT call tools until they give an order number."
            )

        logger.info("%s: requesting opening (%s)", label, "outbound" if is_outbound else "inbound")
        await llm.queue_frame(
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": bootstrap}],
            ),
            FrameDirection.DOWNSTREAM,
        )
        await llm.queue_frame(LLMRunFrame(), FrameDirection.DOWNSTREAM)
        llm._opening_sent = True

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
                    llm._ready_for_realtime_input = True
                    logger.info("%s: restored realtime input after reconnect (mid-call)", label)
                else:
                    llm._ready_for_realtime_input = True
                    logger.warning(
                        "%s: reconnect had empty context; restored input without initial response",
                        label,
                    )
                if outbound_context and getattr(llm, "_opening_sent", False):
                    addr = (outbound_context.delivery_address or "")[:120]
                    summary = outbound_context.last_tool_summary or "continue confirmation"
                    await inject_llm_reminder(
                        llm,
                        "[System — not the customer] Audio reconnected mid-call. "
                        f"Order {outbound_context.order_id}. Continue where you left off. "
                        f"Last action: {summary}. Delivery: {addr}. "
                        "Do NOT repeat 'Hi this is Aria' or ask English/Hindi again.",
                        run=True,
                    )
            except Exception:
                logger.exception("%s: failed to restore Gemini input", label)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport: BaseTransport, websocket) -> None:
        logger.info("%s: client disconnected", label)
        if is_outbound and outbound_context and not outbound_context.outcome_applied:
            from app.services.order_confirmation_service import order_confirmation_service

            asyncio.create_task(
                order_confirmation_service.finalize_call(outbound_context.session_id)
            )

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
