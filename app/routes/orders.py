"""REST API for order operations (testing and integrations)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.order_service import OrderService, order_service

router = APIRouter(prefix="/api/orders", tags=["orders"])


def get_order_service() -> OrderService:
    return order_service


class CancelOrderBody(BaseModel):
    reason: str = Field(default="", description="Cancellation reason")


@router.get("/{order_id}/status")
async def api_order_status(
    order_id: str,
    service: OrderService = Depends(get_order_service),
) -> dict[str, Any]:
    result = await service.get_order_status(order_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "Not found"))
    return result


@router.get("/{order_id}/refund")
async def api_refund_status(
    order_id: str,
    service: OrderService = Depends(get_order_service),
) -> dict[str, Any]:
    result = await service.refund_status(order_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "Not found"))
    return result


@router.post("/{order_id}/cancel")
async def api_cancel_order(
    order_id: str,
    body: CancelOrderBody,
    service: OrderService = Depends(get_order_service),
) -> dict[str, Any]:
    result = await service.cancel_order(order_id, reason=body.reason)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "Not found"))
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message", "Cannot cancel"))
    return result


@router.get("/{order_id}/track")
async def api_track_order(
    order_id: str,
    service: OrderService = Depends(get_order_service),
) -> dict[str, Any]:
    result = await service.track_order(order_id)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=result.get("message", "Not found"))
    return result
