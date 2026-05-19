"""Shopify Admin API — live orders for voice support."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.database.mock_db import Order
from app.utils.address import format_delivery_address
from app.utils.config import Settings

logger = logging.getLogger(__name__)


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
    return _format_date(_add_business_days(datetime.now(timezone.utc), 3))


def _normalize_shop_domain(domain: str) -> str:
    domain = domain.strip().lower().removeprefix("https://").removeprefix("http://")
    domain = domain.split("/")[0]
    if not domain.endswith(".myshopify.com"):
        domain = f"{domain}.myshopify.com"
    return domain


def _parse_order_number(order_id: str) -> list[str]:
    """Build search variants: #1001, 1001, ORD-1001, numeric Shopify id."""
    raw = order_id.strip()
    variants: list[str] = []
    if raw:
        variants.append(raw)
    upper = raw.upper()
    if upper.startswith("ORD-"):
        variants.append(upper[4:])
        variants.append(f"#{upper[4:]}")
    digits = re.sub(r"[^0-9]", "", raw)
    if digits:
        variants.append(digits)
        variants.append(f"#{digits}")
    # dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _map_shopify_status(order: dict[str, Any]) -> str:
    if order.get("cancelled_at"):
        return "cancelled"
    fulfillment = (order.get("fulfillment_status") or "").lower()
    financial = (order.get("financial_status") or "").lower()
    if financial == "refunded":
        return "refunded"
    if fulfillment == "fulfilled":
        return "delivered"
    if fulfillment == "partial":
        return "shipped"
    if fulfillment in ("", "null", "unfulfilled", None):
        return "processing"
    return fulfillment or "processing"


def _refund_status_from_order(order: dict[str, Any], refunds: list[dict[str, Any]]) -> str:
    financial = (order.get("financial_status") or "").lower()
    if financial in ("refunded",):
        return "completed"
    if financial in ("partially_refunded",):
        return "pending"
    if refunds:
        return "pending"
    return "not_requested"


def order_from_shopify(order: dict[str, Any], *, refunds: list[dict[str, Any]] | None = None) -> Order:
    customer = order.get("customer") or {}
    shipping = order.get("shipping_address") or {}
    billing = order.get("billing_address") or {}
    first = customer.get("first_name") or ""
    last = customer.get("last_name") or ""
    name = f"{first} {last}".strip() or str(shipping.get("name") or "").strip() or "Customer"
    delivery_address = format_delivery_address(shipping) or format_delivery_address(billing)

    items = [
        li.get("title") or li.get("name") or "Item"
        for li in (order.get("line_items") or [])
    ]

    fulfillments = order.get("fulfillments") or []
    tracking_id = None
    carrier = None
    for f in fulfillments:
        if f.get("tracking_number"):
            tracking_id = f.get("tracking_number")
            carrier = f.get("tracking_company") or carrier
        for tn in f.get("tracking_numbers") or []:
            tracking_id = tn
    if not tracking_id and fulfillments:
        tracking_id = fulfillments[0].get("tracking_number")
        carrier = fulfillments[0].get("tracking_company")

    status = _map_shopify_status(order)
    refund_status = _refund_status_from_order(order, refunds or [])

    created = order.get("created_at") or ""
    try:
        placed_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        placed_at = datetime.now(timezone.utc)

    currency = order.get("currency") or "USD"
    total = float(order.get("total_price") or 0)

    return Order(
        order_id=str(order.get("name") or order.get("id", "")).lstrip("#").upper(),
        customer_name=name,
        phone=str(customer.get("phone") or order.get("phone") or ""),
        status=status,
        total_inr=total if currency == "INR" else total,
        items=items,
        placed_at=placed_at,
        tracking_id=tracking_id,
        carrier=carrier,
        refund_status=refund_status,
        cancel_allowed=status not in ("cancelled", "delivered", "refunded"),
        delivery_address=delivery_address,
    )


class ShopifyRepository:
    """Read/write orders via Shopify Admin REST API."""

    def __init__(self, settings: Settings) -> None:
        self._shop = _normalize_shop_domain(settings.shopify_shop_domain)
        self._token = settings.shopify_access_token.strip()
        self._version = settings.shopify_api_version.strip() or "2024-10"
        self._timeout = settings.store_api_timeout
        if not self._token:
            raise ValueError("SHOPIFY_ACCESS_TOKEN is required when DATA_SOURCE=shopify")

        self._base = f"https://{self._shop}/admin/api/{self._version}"
        self._headers = {
            "X-Shopify-Access-Token": self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._order_cache: dict[str, dict[str, Any]] = {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, headers=self._headers, params=params, json=json
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()
        except httpx.HTTPStatusError as exc:
            logger.error("Shopify API %s %s → %s", method, path, exc.response.status_code)
            return None
        except httpx.HTTPError as exc:
            logger.error("Shopify API %s %s failed: %s", method, path, exc)
            return None

    def _order_matches(self, order: dict[str, Any], target_digits: str) -> bool:
        name = (order.get("name") or "").lstrip("#").strip()
        order_number = str(order.get("order_number", "")).strip()
        return name == target_digits or order_number == target_digits

    async def _find_raw_order(self, order_id: str) -> dict[str, Any] | None:
        cache_key = order_id.strip().upper()
        if cache_key in self._order_cache:
            return self._order_cache[cache_key]

        digits = re.sub(r"[^0-9]", "", order_id.strip())
        if not digits:
            return None

        # Shopify display name is "#1569"; REST search works best with query=
        search_queries = [
            f"name:#{digits}",
            f"name:{digits}",
            f"#{digits}",
            digits,
        ]
        for q in search_queries:
            data = await self._request(
                "GET",
                "/orders.json",
                params={"status": "any", "limit": 5, "query": q},
            )
            orders = (data or {}).get("orders") or []
            for order in orders:
                if self._order_matches(order, digits):
                    self._order_cache[cache_key] = order
                    return order

        for name_param in (f"#{digits}", digits):
            data = await self._request(
                "GET",
                "/orders.json",
                params={"status": "any", "name": name_param, "limit": 5},
            )
            orders = (data or {}).get("orders") or []
            for order in orders:
                if self._order_matches(order, digits):
                    self._order_cache[cache_key] = order
                    return order

        # Fallback: scan recent orders (handles API search quirks)
        data = await self._request(
            "GET",
            "/orders.json",
            params={"status": "any", "limit": 250, "order": "created_at desc"},
        )
        for order in (data or {}).get("orders") or []:
            if self._order_matches(order, digits):
                self._order_cache[cache_key] = order
                return order

        return None

    async def verify_connection(self) -> tuple[bool, str]:
        """Return (ok, message). Call shop.json to validate domain + token."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._base}/shop.json",
                    headers=self._headers,
                )
                if response.status_code == 200:
                    shop_name = response.json().get("shop", {}).get("name", self._shop)
                    return True, f"Connected to Shopify store: {shop_name}"
                if response.status_code == 401:
                    return False, "Shopify rejected the access token (401). Regenerate SHOPIFY_ACCESS_TOKEN."
                return False, (
                    f"Shopify store not found at {self._shop} (HTTP {response.status_code}). "
                    "Open Shopify Admin → Settings → Domains and copy the "
                    "*.myshopify.com address into SHOPIFY_SHOP_DOMAIN."
                )
        except httpx.HTTPError as exc:
            return False, f"Could not reach Shopify: {exc}"

    async def _shopify_order_id(self, order_id: str) -> int | None:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return None
        return int(raw["id"])

    async def get_order(self, order_id: str) -> Order | None:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return None
        refunds_data = await self._request(
            "GET", f"/orders/{raw['id']}/refunds.json"
        )
        refunds = (refunds_data or {}).get("refunds") or []
        order = order_from_shopify(raw, refunds=refunds)
        order.order_id = str(raw.get("name", order_id)).lstrip("#").upper()
        return order

    async def update_order(self, order_id: str, **fields: Any) -> Order | None:
        if fields.get("status") == "cancelled":
            result = await self.cancel_order_api(order_id, reason=fields.get("reason", ""))
            if result and result.get("success"):
                return await self.get_order(order_id)
        return await self.get_order(order_id)

    async def list_orders(self) -> list[Order]:
        return []

    async def get_refund_status(self, order_id: str) -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {
                "found": False,
                "message": f"No order found with ID {order_id.upper()}.",
            }
        refunds_data = await self._request(
            "GET", f"/orders/{raw['id']}/refunds.json"
        )
        refunds = (refunds_data or {}).get("refunds") or []
        status = _refund_status_from_order(raw, refunds)
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        messages = {
            "not_requested": "No refund has been requested for this order.",
            "pending": f"Refund is being processed and should reflect by {_default_eta_date_str()}.",
            "completed": "Refund has been completed to the original payment method.",
        }
        return {
            "found": True,
            "order_id": display_id,
            "order_status": _map_shopify_status(raw),
            "refund_status": status,
            "message": messages.get(status, f"Refund status: {status}"),
            "refund_count": len(refunds),
        }

    async def get_tracking(self, order_id: str) -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"found": False, "message": "Order not found."}

        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        status = _map_shopify_status(raw)

        fulfill_data = await self._request(
            "GET", f"/orders/{raw['id']}/fulfillments.json"
        )
        fulfillments = (fulfill_data or {}).get("fulfillments") or raw.get("fulfillments") or []

        tracking_id = None
        carrier = None
        tracking_url = None
        for f in fulfillments:
            if f.get("tracking_number"):
                tracking_id = f["tracking_number"]
                carrier = f.get("tracking_company")
                tracking_url = f.get("tracking_url")
                break
            nums = f.get("tracking_numbers") or []
            if nums:
                tracking_id = nums[0]
                companies = f.get("tracking_company") or f.get("tracking_companies")
                carrier = companies[0] if isinstance(companies, list) and companies else companies
                urls = f.get("tracking_urls") or []
                tracking_url = urls[0] if urls else tracking_url
                break

        delivery_address = format_delivery_address(
            raw.get("shipping_address")
        ) or format_delivery_address(raw.get("billing_address"))

        if not tracking_id:
            payload = {
                "found": True,
                "order_id": display_id,
                "status": status,
                "tracking_available": False,
                "message": "Tracking is not available yet. Your order is still being prepared.",
            }
            if delivery_address:
                payload["delivery_address"] = delivery_address
            return payload

        payload = {
            "found": True,
            "order_id": display_id,
            "status": status,
            "tracking_available": True,
            "tracking_id": tracking_id,
            "carrier": carrier or "Courier Partner",
            "tracking_url": tracking_url,
            "estimated_delivery": _default_eta_date_str(),
        }
        if delivery_address:
            payload["delivery_address"] = delivery_address
        return payload

    async def cancel_order_api(self, order_id: str, reason: str = "") -> dict[str, Any]:
        shopify_id = await self._shopify_order_id(order_id)
        if not shopify_id:
            return {"found": False, "success": False, "message": "Order not found."}

        raw = await self._find_raw_order(order_id)
        if raw and raw.get("cancelled_at"):
            display_id = str(raw.get("name", order_id)).lstrip("#").upper()
            return {
                "found": True,
                "success": False,
                "message": "Order is already cancelled.",
                "order_id": display_id,
            }

        if raw and _map_shopify_status(raw) == "delivered":
            display_id = str(raw.get("name", order_id)).lstrip("#").upper()
            return {
                "found": True,
                "success": False,
                "message": "Delivered orders cannot be cancelled via phone support.",
                "order_id": display_id,
            }

        body: dict[str, Any] = {"reason": "customer", "email": False}
        if reason:
            body["note"] = reason

        try:
            await self._request("POST", f"/orders/{shopify_id}/cancel.json", json=body)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200] if exc.response else str(exc)
            return {
                "found": True,
                "success": False,
                "message": f"Shopify could not cancel the order: {detail}",
            }

        display_id = str((raw or {}).get("name", order_id)).lstrip("#").upper()
        self._order_cache.pop(order_id.strip().upper(), None)
        return {
            "found": True,
            "success": True,
            "order_id": display_id,
            "message": "Order cancelled successfully in Shopify.",
            "reason": reason or "Customer request via voice support",
        }
