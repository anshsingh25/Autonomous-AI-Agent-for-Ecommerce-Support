"""Magento order webhooks and confirmation desk APIs (separate from Shopify routes)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

from app.services.magento_order_confirmation_service import (
    MagentoOrderConfirmationService,
    magento_order_confirmation_service,
)
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/magento", tags=["magento"])


def _verify_magento_webhook_secret(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    secret = settings.magento_webhook_secret.strip()
    if not secret:
        return
    header = request.headers.get("X-Magento-Webhook-Secret", "")
    if header != secret:
        raise HTTPException(status_code=401, detail="Invalid Magento webhook secret")


def _require_magento(settings: Settings) -> None:
    if settings.data_source.lower() != "magento":
        raise HTTPException(
            status_code=400,
            detail="Magento routes require DATA_SOURCE=magento on this deployment.",
        )


async def _handle_order_event(
    payload: dict[str, Any],
    service: MagentoOrderConfirmationService,
) -> None:
    try:
        await service.process_magento_order_webhook(payload)
    except Exception:
        logger.exception("Magento order webhook handler failed")


@router.post("/webhooks/order-created")
async def magento_order_created(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    _auth: None = Depends(_verify_magento_webhook_secret),
) -> dict:
    """
    New-order webhook from your Magento module or middleware.

    POST JSON with at least ``increment_id`` or ``entity_id``. Optional ``telephone``.
    Secured with ``MAGENTO_WEBHOOK_SECRET`` header ``X-Magento-Webhook-Secret``.
    """
    _require_magento(settings)
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    background_tasks.add_task(
        _handle_order_event, payload, magento_order_confirmation_service
    )
    return {"status": "accepted"}


@router.post("/confirmation/finalize/{session_id}")
async def finalize_confirmation_session(
    session_id: str,
    settings: Settings = Depends(get_settings),
) -> dict:
    _require_magento(settings)
    return await magento_order_confirmation_service.finalize_call(session_id)


@router.get("/confirmation/pending")
async def list_pending(
    limit: int = Query(default=50, ge=1, le=250),
    settings: Settings = Depends(get_settings),
) -> dict:
    _require_magento(settings)
    return await magento_order_confirmation_service.list_pending_confirmation_orders(
        limit=limit
    )


@router.post("/confirmation/scan-open")
async def scan_open_orders(
    limit: int = Query(default=20, ge=1, le=100),
    dry_run: bool = Query(default=False),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Cron-friendly: dial open Magento orders that still need confirmation."""
    _require_magento(settings)
    return await magento_order_confirmation_service.scan_and_call_unfulfilled(
        limit=limit,
        dry_run=dry_run,
    )
