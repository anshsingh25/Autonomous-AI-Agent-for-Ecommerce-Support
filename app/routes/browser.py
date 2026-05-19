"""Browser-based voice testing (no phone / Twilio required)."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from app.ai.pipeline import run_browser_voice_session
from app.utils.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/browser", tags=["browser"])

_STATIC = Path(__file__).resolve().parent.parent.parent / "static" / "browser"


@router.get("")
@router.get("/")
async def browser_ui() -> FileResponse:
    """Voice test page — microphone + WebSocket audio."""
    return FileResponse(_STATIC / "index.html")


@router.websocket("/ws")
async def browser_voice_ws(websocket: WebSocket) -> None:
    """Realtime audio: browser PCM ↔ Gemini Live."""
    settings = get_settings()
    await websocket.accept()
    logger.info("Browser WebSocket connected")

    try:
        await run_browser_voice_session(websocket, settings)
    except WebSocketDisconnect:
        logger.info("Browser WebSocket disconnected")
    except Exception:
        logger.exception("Browser voice session error")
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.close(code=1011)
