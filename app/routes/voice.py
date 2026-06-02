"""Telephony webhooks (inbound + outbound) and media stream WebSocket."""

from __future__ import annotations

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.ai.pipeline import run_telephony_voice_session
from app.services.telephony_twiml import (
    exotel_inbound_voicebot_ws_url,
    exotel_voicebot_hint,
    twilio_stream_twiml,
)
from app.services.twilio_service import TwilioService
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/voice", tags=["voice"])


async def _exotel_outbound_sid_from_request(
    request: Request, query_session_id: str = ""
) -> str:
    """
    Resolve outbound session id from Exotel's Voicebot HTTPS callback.

    Exotel may send ``CustomField`` in the query string or (commonly) in a
    ``application/x-www-form-urlencoded`` POST body. If we miss it, we return
    an inbound WSS URL without ``session_id`` and the **outbound** call usually
    disconnects right after answer.
    """
    sid = (query_session_id or "").strip()
    if sid:
        return sid
    qp = dict(request.query_params)
    for key in ("CustomField", "custom_field", "session_id", "SessionId"):
        v = (qp.get(key) or "").strip()
        if v:
            return v
    if request.method in ("POST", "PUT", "PATCH"):
        ctype = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in ctype:
            try:
                body = await request.body()
                form_qs = parse_qs(body.decode("utf-8"))
                form = {k: (v[0] if v else "") for k, v in form_qs.items()}
                for key in ("CustomField", "custom_field", "session_id", "SessionId"):
                    v = (form.get(key) or "").strip()
                    if v:
                        return v
            except Exception:
                logger.warning("Could not parse exotel-answer form body", exc_info=True)
    return ""


def get_twilio_service(settings: Settings = Depends(get_settings)) -> TwilioService:
    return TwilioService(settings)


@router.get("/health")
async def voice_health() -> dict:
    return {"status": "ok", "service": "voice"}


@router.get("/exotel-setup")
async def exotel_setup_checklist(settings: Settings = Depends(get_settings)) -> dict:
    """Checklist URLs for Exotel outbound (fixes answer-then-disconnect)."""
    from app.services.outbound_call_service import _public_http_base

    try:
        base = _public_http_base(settings)
    except ValueError:
        base = settings.public_http_url.rstrip("/") if settings.public_http_url else ""

    app_id = getattr(settings, "exotel_outbound_app_id", "") or ""
    account = settings.exotel_account_sid.strip()
    flow_url = ""
    if getattr(settings, "exotel_outbound_flow_url", ""):
        flow_url = settings.exotel_outbound_flow_url.strip()
    elif app_id and account:
        flow_url = f"http://my.exotel.com/{account}/exoml/start_voice/{app_id}"

    from app.services.ngrok_check import detect_ngrok_mismatch

    ngrok_warning = detect_ngrok_mismatch(settings)
    return {
        "voicebot_dynamic_url": f"{base}/voice/outbound/exotel-answer" if base else "",
        "inbound_voicebot_dynamic_url": f"{base}/voice/inbound/exotel-answer" if base else "",
        "inbound_websocket": exotel_inbound_voicebot_ws_url(settings=settings)
        if settings.public_ws_url
        else "",
        "websocket_path": "/voice/ws?session_id=<from CustomField>",
        "exoml_connect_url": flow_url or "SET EXOTEL_OUTBOUND_APP_ID in .env",
        "docs": "/docs/EXOTEL_OUTBOUND_SETUP.md",
        "configured": bool(flow_url and settings.public_ws_url),
        "ngrok_mismatch": ngrok_warning,
    }


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


@router.post("/outbound/answer")
async def outbound_call_answer(
    session_id: str = Query(..., description="Outbound call session from API"),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Twilio fetches this when the customer answers an outbound confirmation call."""
    logger.info("Outbound answer TwiML session_id=%s", session_id)
    twiml = twilio_stream_twiml(session_id, settings=settings)
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.get("/inbound/exotel-answer")
@router.post("/inbound/exotel-answer")
async def inbound_exotel_answer(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Exotel Voicebot dynamic HTTPS URL for **inbound support** (no session_id).

    Configure on your ExoPhone incoming flow Voicebot applet, or use as Calls/connect
    ``Url`` for API test dials (see POST /api/inbound/test-call).
    """
    logger.info("Exotel inbound voicebot URL fetch method=%s", request.method)
    ws_url = exotel_inbound_voicebot_ws_url(settings=settings)
    logger.info("Exotel inbound voicebot WSS → %s", ws_url)
    return JSONResponse({"url": ws_url})


@router.get("/outbound/exotel-answer")
@router.post("/outbound/exotel-answer")
async def outbound_exotel_answer(
    request: Request,
    session_id: str = Query(default=""),
    settings: Settings = Depends(get_settings),
):
    """
    Exotel Voicebot dynamic HTTPS URL — returns ``wss://.../voice/ws?session_id=``.

    Set this URL on the Voicebot applet in App Bazaar (not on Calls/connect ``Url``).
    """
    sid = await _exotel_outbound_sid_from_request(request, session_id)
    qp = dict(request.query_params)
    logger.info(
        "Exotel voicebot URL fetch method=%s session_id=%s query_keys=%s",
        request.method,
        sid or "(missing)",
        sorted(qp.keys()),
    )
    if not sid:
        ws_url = exotel_inbound_voicebot_ws_url(settings=settings)
        logger.warning(
            "Exotel outbound dynamic URL had no CustomField/session_id (query or form). "
            "Falling back to inbound WSS — outbound confirmation calls will misbehave or drop. "
            "Check App Bazaar Voicebot → your HTTPS URL and Exotel CustomField passthrough."
        )
        logger.info(
            "Exotel voicebot (inbound — no session_id / CustomField) → %s",
            ws_url,
        )
        return JSONResponse({"url": ws_url})
    hint = exotel_voicebot_hint(sid, settings=settings)
    ws_url = hint["websocket_url"]
    # Exotel Voicebot dynamic HTTPS must return JSON {"url": "wss://..."} (not plain text).
    logger.info("Exotel voicebot WSS for session_id=%s → %s", sid, ws_url)
    return JSONResponse({"url": ws_url})


@router.post("/outbound/status")
async def outbound_call_status(
    request: Request,
    session_id: str = Query(default=""),
) -> dict:
    """
    Twilio call status callback — marks no-answer / busy as Retry Call.

    Set StatusCallback on outbound dial to this URL with session_id query param.
    """
    body = await request.body()
    form = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
    if not session_id:
        session_id = form.get("session_id", "")
    call_status = (form.get("CallStatus") or "").lower()
    logger.info("Outbound status session=%s CallStatus=%s", session_id, call_status)

    if session_id and call_status in (
        "no-answer",
        "busy",
        "failed",
        "canceled",
        "cancelled",
    ):
        from app.services.confirmation_factory import get_order_confirmation_service

        await get_order_confirmation_service().finalize_call(
            session_id,
            force_no_answer=True,
        )
    return {"status": "ok"}


@router.websocket("/ws")
async def media_stream(
    websocket: WebSocket,
    session_id: str | None = Query(default=None),
) -> None:
    """Telephony media stream WebSocket — inbound support or outbound confirmation."""
    settings = get_settings()
    await websocket.accept()
    if not session_id:
        session_id = websocket.query_params.get("session_id")
    logger.info(
        "WebSocket accepted (session_id=%s)",
        session_id or "(resolve from Exotel start message)",
    )

    try:
        await run_telephony_voice_session(
            websocket, settings, session_id=session_id
        )
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception:
        logger.exception("Voice session error")
        await websocket.close(code=1011)
    else:
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close()
