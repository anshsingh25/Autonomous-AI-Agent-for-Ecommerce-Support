"""Cache shipping address/phone when Shopify API redacts PII on read."""

from __future__ import annotations

from typing import Any

from app.utils.address import format_delivery_address, shipping_address_is_complete

_snapshots: dict[str, dict[str, Any]] = {}


def _order_key(order_id: str) -> str:
    return str(order_id).lstrip("#").strip().upper()


def cache_from_shopify_raw(order_id: str, raw: dict[str, Any]) -> None:
    """Store shipping block from webhook or PUT response when fields are present."""
    ship = raw.get("shipping_address") if isinstance(raw.get("shipping_address"), dict) else None
    if not shipping_address_is_complete(ship):
        return
    key = _order_key(order_id)
    formatted = format_delivery_address(ship)
    phone = str((ship or {}).get("phone") or "").strip()
    if not formatted and not phone:
        return
    _snapshots[key] = {
        "delivery_address": formatted,
        "shipping_phone": phone or _snapshots.get(key, {}).get("shipping_phone"),
    }


def cache_delivery(
    order_id: str,
    *,
    delivery_address: str | None,
    shipping_phone: str | None = None,
) -> None:
    if not delivery_address and not shipping_phone:
        return
    key = _order_key(order_id)
    prev = _snapshots.get(key, {})
    _snapshots[key] = {
        "delivery_address": delivery_address or prev.get("delivery_address"),
        "shipping_phone": shipping_phone or prev.get("shipping_phone"),
    }


def get_cached(order_id: str) -> dict[str, Any]:
    return dict(_snapshots.get(_order_key(order_id), {}))
