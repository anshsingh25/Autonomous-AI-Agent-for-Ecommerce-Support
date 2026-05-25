"""Outbound order-confirmation call API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.order_confirmation_service import (
    OrderConfirmationService,
    order_confirmation_service,
)
from app.services.telephony_twiml import exotel_voicebot_hint
from app.utils.config import Settings, get_settings
from app.utils.exotel_response import (
    http_proxy_status_for_exotel,
    parse_exotel_connect_error_message,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/outbound", tags=["outbound"])


class ConfirmOrderCallBody(BaseModel):
    order_id: str = Field(..., description="Order number, e.g. 1569 or ORD-1001")
    phone: str | None = Field(
        default=None,
        description="Override customer phone (E.164 or 10-digit India). Uses order phone if omitted.",
    )
    force: bool = Field(
        default=False,
        description="Dial even if order already has confirmation tags (testing only)",
    )


def get_confirmation_service() -> OrderConfirmationService:
    return order_confirmation_service


@router.post("/confirm-order")
async def start_confirm_order_call(
    body: ConfirmOrderCallBody,
    service: OrderConfirmationService = Depends(get_confirmation_service),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """
    Check Shopify tags (skip if AI Confirmed / confirmed), then place outbound confirmation call.

    Requires TELEPHONY_PROVIDER (twilio | exotel | mock) and PUBLIC_WS_URL for Exotel.
    """
    try:
        result = await service.dial_order_confirmation(
            body.order_id,
            phone=body.phone,
            force=body.force,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Outbound dial failed")
        raise HTTPException(status_code=502, detail=f"Could not place call: {exc}") from exc

    if (
        settings.telephony_provider.lower() == "exotel"
        and result.get("session_id")
        and result.get("status") == "dialing"
    ):
        result["exotel_setup"] = exotel_voicebot_hint(result["session_id"], settings=settings)

    if result.get("status") == "error":
        msg = str(result.get("message") or "")
        code, detail = parse_exotel_connect_error_message(msg)
        if code is not None:
            raise HTTPException(
                status_code=http_proxy_status_for_exotel(code),
                detail=detail[:800] if detail else msg[:800],
            )

    return result
