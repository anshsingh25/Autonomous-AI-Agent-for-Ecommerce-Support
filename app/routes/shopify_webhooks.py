"""Shopify webhooks — auto-trigger outbound confirmation calls."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from app.services.order_confirmation_service import (
    OrderConfirmationService,
    order_confirmation_service,
)
from app.utils.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/shopify", tags=["shopify"])


def _verify_shopify_hmac(body: bytes, hmac_header: str | None, secret: str) -> bool:
    if not secret:
        return True
    if not hmac_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)


async def _handle_order_event(payload: dict, service: OrderConfirmationService) -> None:
    try:
        result = await service.process_shopify_order_webhook(payload)
        logger.info("Shopify order webhook result: %s", result)
    except Exception:
        logger.exception("Shopify order webhook handler failed")


@router.post("/webhooks/orders-create")
async def shopify_orders_create(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Shopify `orders/create` webhook — queue confirmation call for unfulfilled orders.

    Register in Shopify Admin → Settings → Notifications → Webhooks.
    """
    settings = get_settings()
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not _verify_shopify_hmac(body, hmac_header, settings.shopify_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid Shopify HMAC")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    background_tasks.add_task(_handle_order_event, payload, order_confirmation_service)
    return {"status": "accepted"}


@router.post("/webhooks/orders-paid")
async def shopify_orders_paid(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """Shopify `orders/paid` — preferred trigger after payment succeeds."""
    settings = get_settings()
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256")
    if not _verify_shopify_hmac(body, hmac_header, settings.shopify_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid Shopify HMAC")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    background_tasks.add_task(_handle_order_event, payload, order_confirmation_service)
    return {"status": "accepted"}


@router.post("/confirmation/finalize/{session_id}")
async def finalize_confirmation_session(session_id: str) -> dict:
    """Manually finalize a call (testing) — applies tags from collected transcript."""
    return await order_confirmation_service.finalize_call(session_id)


@router.get("/confirmation/pending")
async def list_pending_confirmation_orders(
    limit: int = Query(default=50, ge=1, le=250),
) -> dict:
    """
    List unfulfilled Shopify orders that still need an outbound confirmation call.

    Skips orders tagged AI Confirmed, confirmed, Proceed+confirmed, cancelled, or in progress.
    """
    try:
        return await order_confirmation_service.list_pending_confirmation_orders(
            limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/confirmation/call-unconfirmed")
async def call_unconfirmed_orders(
    limit: int = Query(default=30, ge=1, le=50),
) -> dict:
    """
    Place outbound confirmation calls for all unconfirmed unfulfilled orders that have a phone.

    Orders without a phone in the Shopify API are listed in ``need_phone`` — use the desk UI
    to enter numbers and call individually.
    """
    try:
        return await order_confirmation_service.call_all_unconfirmed(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/confirmation/scan-unfulfilled")
async def scan_unfulfilled_orders(
    limit: int = Query(default=20, ge=1, le=50),
    dry_run: bool = Query(
        default=False,
        description="If true, only list orders that would be dialed (no calls placed)",
    ),
) -> dict:
    """
    Scan recent unfulfilled orders without confirmation tags and place outbound calls.

    Requires DATA_SOURCE=shopify. Respects AUTO_CONFIRMATION_ENABLED per order dial.
    """
    try:
        return await order_confirmation_service.scan_and_call_unfulfilled(
            limit=limit,
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
