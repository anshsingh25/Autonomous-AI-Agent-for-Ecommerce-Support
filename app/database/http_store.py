"""HTTP client for your live ecommerce store REST API."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from app.database.mock_db import Order
from app.utils.address import format_delivery_address
from app.utils.config import Settings

logger = logging.getLogger(__name__)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now()


def _first(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def order_from_api_payload(data: dict[str, Any]) -> Order:
    """Map your API JSON to the internal Order model (field names are configurable)."""
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    items = _first(data, "items", "line_items", default=[])
    if items and isinstance(items[0], dict):
        items = [
            i.get("name") or i.get("title") or str(i.get("sku", i))
            for i in items
        ]
    elif not isinstance(items, list):
        items = [str(items)]

    tracking = data.get("tracking") if isinstance(data.get("tracking"), dict) else {}
    ship = data.get("shipping_address")
    if not isinstance(ship, dict):
        ship = data.get("delivery_address") if isinstance(data.get("delivery_address"), dict) else None
    delivery = format_delivery_address(ship)
    if not delivery:
        raw_delivery = _first(data, "delivery_address", "shipping_address", default="")
        if isinstance(raw_delivery, str) and raw_delivery.strip():
            delivery = raw_delivery.strip()

    return Order(
        order_id=str(_first(data, "order_id", "id", "order_number", default="")).upper(),
        customer_name=str(
            _first(data, "customer_name", default=customer.get("name", "Customer"))
        ),
        phone=str(_first(data, "phone", default=customer.get("phone", ""))),
        status=str(_first(data, "status", "order_status", default="unknown")).lower(),
        total_inr=float(_first(data, "total_inr", "total", "amount", default=0) or 0),
        items=[str(i) for i in items],
        placed_at=_parse_datetime(_first(data, "placed_at", "created_at", "date")),
        tracking_id=_first(data, "tracking_id", default=tracking.get("id")),
        carrier=_first(data, "carrier", default=tracking.get("carrier")),
        refund_status=str(_first(data, "refund_status", default="not_requested")).lower(),
        cancel_allowed=bool(_first(data, "cancel_allowed", default=True)),
        delivery_address=delivery,
    )


class HttpStoreRepository:
    """
    Calls your store backend over HTTPS.

    Expected routes (override paths in .env if yours differ):
      GET  {base}/orders/{id}
      GET  {base}/orders/{id}/refund
      POST {base}/orders/{id}/cancel
      GET  {base}/orders/{id}/tracking
    """

    def __init__(self, settings: Settings) -> None:
        self._base = settings.store_api_base_url.rstrip("/")
        self._timeout = settings.store_api_timeout
        self._headers: dict[str, str] = {"Accept": "application/json"}
        if settings.store_api_key:
            self._headers["Authorization"] = f"Bearer {settings.store_api_key}"
        if settings.store_api_header_name and settings.store_api_key:
            # e.g. X-API-Key for some platforms
            self._headers[settings.store_api_header_name] = settings.store_api_key

        self._path_order = settings.store_api_path_order
        self._path_refund = settings.store_api_path_refund
        self._path_cancel = settings.store_api_path_cancel
        self._path_track = settings.store_api_path_track

    def _url(self, path: str, order_id: str) -> str:
        return f"{self._base}{path.format(order_id=order_id.upper())}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, headers=self._headers, json=json
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("Store API %s %s failed: %s", method, url, exc)
            raise

    async def get_order(self, order_id: str) -> Order | None:
        data = await self._request("GET", self._url(self._path_order, order_id))
        if not data:
            return None
        if data.get("found") is False:
            return None
        payload = data.get("order", data)
        if not isinstance(payload, dict):
            return None
        return order_from_api_payload(payload)

    async def update_order(self, order_id: str, **fields: Any) -> Order | None:
        body = {k: v for k, v in fields.items()}
        data = await self._request(
            "POST",
            self._url(self._path_cancel, order_id),
            json=body if body else {"reason": "Customer request via voice support"},
        )
        if data is None:
            return None
        if data.get("success") is False:
            return await self.get_order(order_id)
        return await self.get_order(order_id)

    async def list_orders(self) -> list[Order]:
        return []

    async def fetch_refund(self, order_id: str) -> dict[str, Any] | None:
        return await self._request("GET", self._url(self._path_refund, order_id))

    async def fetch_tracking(self, order_id: str) -> dict[str, Any] | None:
        return await self._request("GET", self._url(self._path_track, order_id))

    async def cancel_order_api(
        self, order_id: str, reason: str = ""
    ) -> dict[str, Any] | None:
        return await self._request(
            "POST",
            self._url(self._path_cancel, order_id),
            json={"reason": reason or "Customer request via voice support"},
        )
