"""Twilio voice webhook and WebSocket media stream routes."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.ai.pipeline import run_twilio_voice_session
from app.services.twilio_service import TwilioService
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])


def get_twilio_service(settings: Settings = Depends(get_settings)) -> TwilioService:
    return TwilioService(settings)


@router.get("/health")
async def voice_health() -> dict:
    return {"status": "ok", "service": "voice"}


@router.post("/incoming")
async def incoming_call(
    request: Request,
    settings: Settings = Depends(get_settings),
    twilio: TwilioService = Depends(get_twilio_service),
) -> HTMLResponse:
    """
    Twilio voice webhook — returns TwiML to open a Media Stream WebSocket.

    Configure in Twilio Console: Phone Number → Voice → A call comes in → Webhook URL
    POST https://<your-host>/voice/incoming
    """
    body = await request.body()
    form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    call_sid = form.get("CallSid", "unknown")
    from_number = form.get("From", "unknown")
    logger.info("Incoming call | CallSid=%s From=%s", call_sid, from_number)

    try:
        ws_url = twilio.resolve_websocket_url()
    except ValueError as exc:
        logger.error(str(exc))
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?><Response><Say>Service is not configured. Please try again later.</Say></Response>',
            media_type="application/xml",
            status_code=503,
        )

    twiml = twilio.incoming_call_twiml(ws_url)
    logger.debug("Connecting stream to %s", ws_url)
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.websocket("/ws")
async def media_stream(websocket: WebSocket) -> None:
    """Twilio Media Streams WebSocket — realtime audio with Pipecat."""
    settings = get_settings()
    await websocket.accept()
    logger.info("WebSocket accepted for media stream")

    try:
        await run_twilio_voice_session(websocket, settings)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Voice session error")
        await websocket.close(code=1011)
    else:
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close()
