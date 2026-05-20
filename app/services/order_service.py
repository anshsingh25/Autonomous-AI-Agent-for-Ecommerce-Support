"""Ecommerce order operations used by REST API and voice AI tools."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.database.factory import create_order_repository
from app.database.http_store import HttpStoreRepository
from app.database.shopify_store import ShopifyRepository
from app.database.protocol import OrderRepository
from app.utils.config import get_settings


def _format_date(dt: datetime) -> str:
    return dt.strftime("%d %b %Y")


def _add_business_days(start: datetime, business_days: int) -> datetime:
    current = start
    added = 0
    while added < business_days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def _default_eta_date_str() -> str:
    now = datetime.now(timezone.utc)
    return _format_date(_add_business_days(now, 3))


def _delivery_address_fields(order) -> dict[str, Any]:
    """Include delivery address in tool JSON when present on the order."""
    if order.delivery_address:
        return {"delivery_address": order.delivery_address}
    return {
        "delivery_address": None,
        "message_no_address": "No delivery address is on file for this order.",
    }


class OrderService:
    """Business logic for order lookup, tracking, refunds, and cancellation."""

    def __init__(self, database: OrderRepository | None = None) -> None:
        self._db = database or create_order_repository(get_settings())

    async def get_customer_phone(self, order_id: str) -> str | None:
        """Phone on file for outbound calls."""
        order = await self._db.get_order(order_id)
        if not order or not order.phone:
            return None
        return order.phone.strip() or None

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        order = await self._db.get_order(order_id)
        if not order:
            return {
                "found": False,
                "message": f"No order found with ID {order_id.upper()}.",
            }
        payload = {
            "found": True,
            "order_id": order.order_id,
            "status": order.status,
            "customer_name": order.customer_name,
            "items": order.items,
            "total_inr": order.total_inr,
            "placed_at": order.placed_at.isoformat(),
        }
        payload.update(_delivery_address_fields(order))
        return payload

    async def refund_status(self, order_id: str) -> dict[str, Any]:
        if isinstance(self._db, ShopifyRepository):
            return await self._db.get_refund_status(order_id)
        if isinstance(self._db, HttpStoreRepository):
            raw = await self._db.fetch_refund(order_id)
            if raw is not None:
                return _normalize_refund_response(order_id, raw)

        order = await self._db.get_order(order_id)
        if not order:
            return {"found": False, "message": f"No order found with ID {order_id.upper()}."}
        return {
            "found": True,
            "order_id": order.order_id,
            "order_status": order.status,
            "refund_status": order.refund_status or "not_requested",
            "message": _refund_message(order.refund_status or "not_requested"),
        }

    async def cancel_order(self, order_id: str, reason: str = "") -> dict[str, Any]:
        if isinstance(self._db, (ShopifyRepository, HttpStoreRepository)):
            raw = await self._db.cancel_order_api(order_id, reason=reason)
            if raw is not None:
                return _normalize_cancel_response(order_id, raw)

        order = await self._db.get_order(order_id)
        if not order:
            return {"found": False, "success": False, "message": "Order not found."}
        if not order.cancel_allowed:
            return {
                "found": True,
                "success": False,
                "message": "This order cannot be cancelled (already delivered or refunded).",
            }
        if order.status in ("cancelled", "delivered"):
            return {
                "found": True,
                "success": False,
                "message": f"Order is already {order.status}.",
            }
        await self._db.update_order(order_id, status="cancelled", cancel_allowed=False)
        return {
            "found": True,
            "success": True,
            "order_id": order.order_id,
            "message": "Order cancelled successfully.",
            "reason": reason or "Customer request via phone support",
        }

    async def track_order(self, order_id: str) -> dict[str, Any]:
        if isinstance(self._db, ShopifyRepository):
            return await self._db.get_tracking(order_id)
        if isinstance(self._db, HttpStoreRepository):
            raw = await self._db.fetch_tracking(order_id)
            if raw is not None:
                return _normalize_track_response(order_id, raw)

        order = await self._db.get_order(order_id)
        if not order:
            return {"found": False, "message": "Order not found."}
        if not order.tracking_id:
            payload = {
                "found": True,
                "order_id": order.order_id,
                "status": order.status,
                "tracking_available": False,
                "message": "Tracking is not available yet. Your order is still being prepared.",
            }
            payload.update(_delivery_address_fields(order))
            return payload
        payload = {
            "found": True,
            "order_id": order.order_id,
            "status": order.status,
            "tracking_available": True,
            "tracking_id": order.tracking_id,
            "carrier": order.carrier,
            "estimated_delivery": _default_eta_date_str(),
        }
        payload.update(_delivery_address_fields(order))
        return payload


def _normalize_refund_response(order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("found") is False:
        return {"found": False, "message": raw.get("message", "Order not found.")}
    status = str(raw.get("refund_status", raw.get("status", "not_requested"))).lower()
    return {
        "found": True,
        "order_id": raw.get("order_id", order_id.upper()),
        "order_status": raw.get("order_status", ""),
        "refund_status": status,
        "message": raw.get("message") or _refund_message(status),
    }


def _normalize_cancel_response(order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("found") is False:
        return {"found": False, "success": False, "message": raw.get("message", "Order not found.")}
    return {
        "found": True,
        "success": bool(raw.get("success", True)),
        "order_id": raw.get("order_id", order_id.upper()),
        "message": raw.get("message", "Order cancelled."),
        "reason": raw.get("reason", ""),
    }


def _normalize_track_response(order_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("found") is False:
        return {"found": False, "message": raw.get("message", "Order not found.")}
    if raw.get("tracking_available") is False:
        return {
            "found": True,
            "order_id": raw.get("order_id", order_id.upper()),
            "status": raw.get("status", ""),
            "tracking_available": False,
            "message": raw.get("message", "Tracking not available yet."),
        }
    return {
        "found": True,
        "order_id": raw.get("order_id", order_id.upper()),
        "status": raw.get("status", ""),
        "tracking_available": True,
        "tracking_id": raw.get("tracking_id", ""),
        "carrier": raw.get("carrier", ""),
        "estimated_delivery": raw.get("estimated_delivery") or _default_eta_date_str(),
    }


def _refund_message(refund_status: str) -> str:
    eta = _default_eta_date_str()
    messages = {
        "not_requested": "No refund has been requested for this order.",
        "pending": f"Refund is being processed and should reflect by {eta}.",
        "completed": "Refund has been completed to your original payment method.",
        "rejected": "Refund request was rejected. Please contact support for details.",
    }
    return messages.get(refund_status, f"Refund status: {refund_status}")


def build_order_service() -> OrderService:
    return OrderService(create_order_repository(get_settings()))


order_service = build_order_service()
