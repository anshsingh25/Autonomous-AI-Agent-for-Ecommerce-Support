"""Browser-based voice testing (no phone / Twilio required)."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.ai.pipeline import run_browser_outbound_session, run_browser_voice_session
from app.services.outbound_call_service import outbound_call_service
from app.utils.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/browser", tags=["browser"])

_STATIC = Path(__file__).resolve().parent.parent.parent / "static" / "browser"
_ADMIN_STATIC = Path(__file__).resolve().parent.parent.parent / "static" / "admin"
class OutboundPrepareBody(BaseModel):
    order_id: str = Field(..., min_length=1, description="Shopify order name/number or mock ORD-*")
    phone: str | None = Field(None, description="Optional E.164 phone; placeholder used if omitted")


@router.get("")
@router.get("/")
async def browser_ui() -> FileResponse:
    """Voice test page — microphone + WebSocket audio."""
    return FileResponse(_STATIC / "index.html")


@router.get("/support")
async def browser_support_ui() -> FileResponse:
    """Customer voice support (Magento footer) — same UI as /browser."""
    return FileResponse(_STATIC / "index.html")


@router.get("/outbound")
async def browser_outbound_ui() -> FileResponse:
    """Simulate outbound order confirmation in the browser (no dial)."""
    return FileResponse(_STATIC / "outbound.html")


@router.get("/confirmation-desk")
async def confirmation_calls_desk() -> FileResponse:
    """Agent desk: list unconfirmed Shopify orders and place Exotel outbound calls."""
    return FileResponse(_ADMIN_STATIC / "confirmation_calls.html")


@router.post("/outbound/prepare")
async def browser_outbound_prepare(body: OutboundPrepareBody) -> dict:
    """Load order and create session_id for browser outbound WebSocket."""
    try:
        return await outbound_call_service.prepare_browser_session(
            body.order_id.strip(),
            phone=body.phone.strip() if body.phone else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Outbound browser prepare failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.websocket("/ws")
async def browser_voice_ws(websocket: WebSocket) -> None:
    """Realtime audio: browser PCM ↔ Gemini Live."""
    settings = get_settings()
    await websocket.accept()
    logger.info("Browser WebSocket connected")

    inbound_session_id = (
        websocket.query_params.get("session_id", "").strip() or str(uuid.uuid4())
    )
    try:
        await run_browser_voice_session(
            websocket, settings, inbound_session_id=inbound_session_id
        )
    except WebSocketDisconnect:
        logger.info("Browser WebSocket disconnected")
    except Exception:
        logger.exception("Browser voice session error")
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=1011)


@router.websocket("/outbound/ws")
async def browser_outbound_voice_ws(websocket: WebSocket, session_id: str) -> None:
    """Browser PCM ↔ outbound confirmation call (requires prepare step)."""
    settings = get_settings()
    if not session_id.strip():
        await websocket.close(code=4400, reason="session_id required")
        return

    await websocket.accept()
    logger.info("Browser outbound WebSocket session_id=%s", session_id)

    try:
        await run_browser_outbound_session(
            websocket, settings, session_id=session_id.strip()
        )
    except ValueError as exc:
        logger.warning("Browser outbound rejected: %s", exc)
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=4404, reason=str(exc)[:120])
    except WebSocketDisconnect:
        logger.info("Browser outbound WebSocket disconnected session_id=%s", session_id)
    except Exception:
        logger.exception("Browser outbound voice session error")
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=1011)
