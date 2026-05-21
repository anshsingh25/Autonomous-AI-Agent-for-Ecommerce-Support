"""Inbound support call API (Exotel test dial)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.outbound_call_service import (
    _format_exotel_from,
    exotel_inbound_flow_url,
)
from app.services.telephony_twiml import exotel_inbound_voicebot_ws_url
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/inbound", tags=["inbound"])


class InboundTestCallBody(BaseModel):
    phone: str = Field(
        ...,
        description="Customer phone to ring (E.164 or 10-digit India)",
    )


@router.post("/test-call")
async def place_inbound_test_call(
    body: InboundTestCallBody,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """
    Ring the customer and connect to **inbound support** Aria (not order confirmation).

    Uses Exotel Calls/connect with an **ExoML app** URL (not the HTTPS answer URL).
    Voicebot in that app must use dynamic HTTPS → /voice/inbound/exotel-answer
    (or /voice/outbound/exotel-answer — works without session_id too).
    """
    if settings.telephony_provider.lower() != "exotel":
        raise HTTPException(
            status_code=400,
            detail="TELEPHONY_PROVIDER must be exotel for this endpoint",
        )

    sid = settings.exotel_account_sid.strip()
    token = settings.exotel_api_token.strip()
    api_key = settings.exotel_api_key.strip()
    from_num = settings.exotel_caller_id.strip()
    if not all([sid, token, api_key, from_num]):
        raise HTTPException(
            status_code=400,
            detail="EXOTEL_ACCOUNT_SID, API key/token, and EXOTEL_CALLER_ID required",
        )

    try:
        connect_url = exotel_inbound_flow_url(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    customer_from = _format_exotel_from(body.phone.strip())
    params = {
        "From": customer_from,
        "CallerId": from_num,
        "CallType": "trans",
        "Url": connect_url,
    }
    api_url = f"{settings.exotel_api_host.rstrip('/')}/v1/Accounts/{sid}/Calls/connect"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                api_url, params=params, auth=(api_key, token)
            )
    except httpx.HTTPError as exc:
        logger.exception("Exotel inbound test dial failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if response.status_code >= 400:
        detail = (response.text or "")[:500]
        logger.error("Exotel inbound connect failed %s: %s", response.status_code, detail)
        raise HTTPException(
            status_code=502,
            detail=f"Exotel connect failed ({response.status_code}): {detail}",
        )

    logger.info(
        "Exotel inbound test call From=%s CallerId=%s Url=%s",
        customer_from,
        from_num,
        connect_url,
    )
    return {
        "status": "dialing",
        "mode": "inbound_support",
        "to": customer_from,
        "caller_id": from_num,
        "provider": "exotel",
        "exoml_connect_url": connect_url,
        "voicebot_dynamic_url_hint": exotel_inbound_voicebot_ws_url(settings=settings),
        "websocket_url": exotel_inbound_voicebot_ws_url(settings=settings),
        "provider_response": (response.text or "")[:200],
    }
