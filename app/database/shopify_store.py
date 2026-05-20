"""Shopify Admin API — live orders for voice support."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.database.mock_db import Order
from app.models.order_line_item import OrderLineItemSnapshot
from app.services.shopify_tags import order_should_skip_auto_call
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
            body = ""
            try:
                body = (exc.response.text or "")[:500]
            except Exception:
                pass
            logger.error(
                "Shopify API %s %s → %s %s",
                method,
                path,
                exc.response.status_code,
                body,
            )
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

    @staticmethod
    def parse_tags(tags_value: str | None) -> list[str]:
        return [t.strip() for t in (tags_value or "").split(",") if t.strip()]

    def _invalidate_order_cache(self, order_id: str) -> None:
        self._order_cache.pop(order_id.strip().upper(), None)

    async def get_raw_order_by_shopify_id(self, shopify_id: int | str) -> dict[str, Any] | None:
        data = await self._request("GET", f"/orders/{shopify_id}.json")
        order = (data or {}).get("order")
        if order:
            key = str(order.get("name", order.get("id", ""))).lstrip("#").upper()
            if key:
                self._order_cache[key] = order
        return order

    async def get_raw_order(self, order_id: str) -> dict[str, Any] | None:
        return await self._find_raw_order(order_id)

    @staticmethod
    def is_unfulfilled(raw: dict[str, Any]) -> bool:
        if raw.get("cancelled_at"):
            return False
        status = (raw.get("fulfillment_status") or "").strip().lower()
        return status in ("", "unfulfilled", "null", "none")

    @staticmethod
    def extract_phone(raw: dict[str, Any]) -> str:
        """Best-effort phone from order, customer, or shipping/billing address."""
        for source in (
            raw.get("phone"),
            (raw.get("customer") or {}).get("phone"),
            (raw.get("shipping_address") or {}).get("phone"),
            (raw.get("billing_address") or {}).get("phone"),
            (raw.get("customer") or {}).get("default_address", {}).get("phone"),
        ):
            if source and str(source).strip():
                return str(source).strip()
        return ""

    @staticmethod
    def phone_missing_reason(raw: dict[str, Any]) -> str:
        """
        Explain why phone is empty — often Shopify Basic hides PII from the API.
        """
        if ShopifyRepository.extract_phone(raw):
            return ""
        ship = raw.get("shipping_address") or {}
        if ship and not ship.get("phone") and not ship.get("address1"):
            return (
                "No phone in Shopify API (shipping address incomplete on order). "
                "Phone may show in Admin but is not returned to apps without protected "
                "customer data access — pass \"phone\" in POST /api/outbound/confirm-order."
            )
        return (
            "No phone in Shopify API response (protected customer data / plan limits). "
            "Pass \"phone\" in POST /api/outbound/confirm-order."
        )

    def order_needs_confirmation(self, raw: dict[str, Any]) -> tuple[bool, str]:
        """Unfulfilled and not yet AI-confirmed (ignores whether phone is on the order)."""
        if not self.is_unfulfilled(raw):
            return False, "Order is not unfulfilled"
        if raw.get("cancelled_at"):
            return False, "Order is cancelled"
        financial = (raw.get("financial_status") or "").lower()
        if financial in ("voided", "refunded"):
            return False, f"Financial status is {financial}"
        tags = set(self.parse_tags(raw.get("tags")))
        if order_should_skip_auto_call(tags):
            return False, f"Order already confirmed or in progress: {', '.join(sorted(tags))}"
        return True, "needs_confirmation"

    def is_eligible_for_confirmation_call(self, raw: dict[str, Any]) -> tuple[bool, str]:
        """Whether we should auto-dial for order confirmation (unfulfilled, not confirmed)."""
        needs, reason = self.order_needs_confirmation(raw)
        if not needs:
            return False, reason
        if not self.extract_phone(raw):
            return False, self.phone_missing_reason(raw)
        return True, "eligible"

    def needs_confirmation_call(self, raw: dict[str, Any]) -> bool:
        """True if this order should receive an outbound confirmation call."""
        needs, _ = self.order_needs_confirmation(raw)
        return needs

    async def _graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Admin GraphQL API — preferred for tag updates (REST PUT often returns 422)."""
        url = f"https://{self._shop}/admin/api/{self._version}/graphql.json"
        body: dict[str, Any] = {"query": query}
        if variables:
            body["variables"] = variables
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, headers=self._headers, json=body)
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    logger.error("Shopify GraphQL errors: %s", payload["errors"])
                    return None
                return payload.get("data") or {}
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "")[:500]
            logger.error("Shopify GraphQL → %s %s", exc.response.status_code, detail)
            return None
        except httpx.HTTPError as exc:
            logger.error("Shopify GraphQL failed: %s", exc)
            return None

    async def _append_tags_graphql(
        self, shopify_id: int, tags: list[str]
    ) -> tuple[bool, str]:
        """Add tags via tagsAdd mutation (does not replace full tag list)."""
        if not tags:
            return True, "no new tags"
        data = await self._graphql(
            """
            mutation tagsAdd($id: ID!, $tags: [String!]!) {
              tagsAdd(id: $id, tags: $tags) {
                node { id }
                userErrors { field message }
              }
            }
            """,
            {
                "id": f"gid://shopify/Order/{shopify_id}",
                "tags": tags,
            },
        )
        if not data:
            return False, "GraphQL request failed"
        result = (data.get("tagsAdd") or {})
        errors = result.get("userErrors") or []
        if errors:
            messages = "; ".join(
                str(e.get("message", e)) for e in errors if isinstance(e, dict)
            )
            return False, messages or "tagsAdd userErrors"
        if result.get("node"):
            return True, "tagsAdd ok"
        return False, "tagsAdd returned no node"

    async def _append_tags_rest(
        self, shopify_id: int, merged_tags: list[str]
    ) -> tuple[bool, str]:
        """Fallback: replace full tag string via REST (can 422 on closed/archived orders)."""
        payload = {"order": {"id": shopify_id, "tags": ", ".join(merged_tags)}}
        result = await self._request("PUT", f"/orders/{shopify_id}.json", json=payload)
        if result is None:
            return False, "REST tag update failed"
        return True, "REST ok"

    async def append_tags(self, order_id: str, tags_to_add: list[str]) -> dict[str, Any]:
        """Append tags without removing existing ones."""
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"success": False, "message": "Order not found"}

        existing = self.parse_tags(raw.get("tags"))
        merged: list[str] = list(existing)
        added: list[str] = []
        for tag in tags_to_add:
            tag = tag.strip()
            if tag and tag not in merged:
                merged.append(tag)
                added.append(tag)

        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        if not added:
            return {
                "success": True,
                "order_id": display_id,
                "tags": merged,
                "added": [],
                "message": "Tags already present",
            }

        shopify_id = int(raw["id"])
        ok, detail = await self._append_tags_graphql(shopify_id, added)
        method = "graphql"
        if not ok:
            logger.warning(
                "Shopify tagsAdd failed order=%s (%s); trying REST fallback",
                display_id,
                detail,
            )
            ok, detail = await self._append_tags_rest(shopify_id, merged)
            method = "rest"
        if not ok:
            return {
                "success": False,
                "order_id": display_id,
                "message": f"Shopify tag update failed: {detail}",
                "added": [],
            }

        self._invalidate_order_cache(order_id)
        logger.info(
            "Shopify tags appended order=%s added=%s via=%s",
            display_id,
            added,
            method,
        )
        return {
            "success": True,
            "order_id": display_id,
            "tags": merged,
            "added": added,
            "method": method,
        }

    async def list_recent_unfulfilled_orders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/orders.json",
            params={
                "status": "open",
                "fulfillment_status": "unfulfilled",
                "limit": min(limit, 250),
            },
        )
        return (data or {}).get("orders") or []

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

    @staticmethod
    def line_items_from_raw(raw: dict[str, Any]) -> list[OrderLineItemSnapshot]:
        snapshots: list[OrderLineItemSnapshot] = []
        for li in raw.get("line_items") or []:
            lid = li.get("id")
            if lid is None:
                continue
            snapshots.append(
                OrderLineItemSnapshot(
                    line_item_id=int(lid),
                    title=str(li.get("title") or li.get("name") or "Item"),
                    quantity=int(li.get("quantity") or 1),
                    variant_title=(li.get("variant_title") or None),
                    sku=(li.get("sku") or None),
                    product_id=int(li["product_id"]) if li.get("product_id") else None,
                    variant_id=int(li["variant_id"]) if li.get("variant_id") else None,
                )
            )
        return snapshots

    def _pick_raw_line_item(
        self,
        raw: dict[str, Any],
        *,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any] | None:
        lines = raw.get("line_items") or []
        if not lines:
            return None
        needle = (product_name or "").strip().lower()
        if needle:
            for li in lines:
                title = str(li.get("title") or "").lower()
                variant = str(li.get("variant_title") or "").lower()
                if needle in title or needle in variant:
                    return li
        idx = max(0, min(line_index, len(lines) - 1))
        return lines[idx]

    @staticmethod
    def _variant_search_text(variant: dict[str, Any]) -> str:
        parts = [
            variant.get("title"),
            variant.get("option1"),
            variant.get("option2"),
            variant.get("option3"),
        ]
        return " ".join(str(p).strip() for p in parts if p).lower()

    def _match_product_variant(
        self, variants: list[dict[str, Any]], query: str
    ) -> dict[str, Any] | None:
        """Find variant by size/colour/title phrase (e.g. 'Medium', 'Black')."""
        q = (query or "").strip().lower()
        if not q:
            return None

        tokens = [t for t in re.split(r"[\s,/|]+", q) if t]
        candidates: list[tuple[int, dict[str, Any]]] = []

        for variant in variants:
            text = self._variant_search_text(variant)
            if q == text or q in text:
                candidates.append((100, variant))
                continue
            score = sum(1 for tok in tokens if tok in text)
            if score and score >= len(tokens):
                candidates.append((score * 10, variant))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    async def _fetch_product_variants(self, product_id: int) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/products/{product_id}.json")
        product = (data or {}).get("product") or {}
        out: list[dict[str, Any]] = []
        for v in product.get("variants") or []:
            inv = v.get("inventory_quantity")
            try:
                inv_n = int(inv) if inv is not None else 0
            except (TypeError, ValueError):
                inv_n = 0
            policy = str(v.get("inventory_policy") or "")
            out.append(
                {
                    "variant_id": int(v["id"]),
                    "title": v.get("title"),
                    "option1": v.get("option1"),
                    "option2": v.get("option2"),
                    "option3": v.get("option3"),
                    "price": float(v.get("price") or 0),
                    "inventory_quantity": inv_n,
                    "in_stock": inv_n > 0 or policy == "continue",
                    "display": " / ".join(
                        x
                        for x in (
                            v.get("option1"),
                            v.get("option2"),
                            v.get("option3"),
                        )
                        if x
                    )
                    or v.get("title"),
                }
            )
        return out

    async def _order_edit_begin(
        self, shopify_id: int
    ) -> tuple[str | None, list[dict[str, Any]], str | None]:
        """Return (calculated_order_id, line_nodes, error_message)."""
        order_gid = f"gid://shopify/Order/{shopify_id}"
        begin = await self._graphql(
            """
            mutation orderEditBegin($id: ID!) {
              orderEditBegin(id: $id) {
                calculatedOrder {
                  id
                  lineItems(first: 25) {
                    edges {
                      node { id quantity title variantTitle }
                    }
                  }
                }
                userErrors { message }
              }
            }
            """,
            {"id": order_gid},
        )
        if not begin:
            return None, [], "Could not start order edit in Shopify"
        block = begin.get("orderEditBegin") or {}
        if block.get("userErrors"):
            return None, [], block["userErrors"][0].get("message", "orderEditBegin failed")
        calc = block.get("calculatedOrder")
        if not calc:
            return None, [], "orderEditBegin returned no calculated order"
        edges = (calc.get("lineItems") or {}).get("edges") or []
        nodes = [e.get("node") for e in edges if e.get("node")]
        return calc["id"], nodes, None

    async def _order_edit_commit(
        self, calc_id: str
    ) -> tuple[bool, str | None]:
        commit = await self._graphql(
            """
            mutation orderEditCommit($id: ID!) {
              orderEditCommit(id: $id) {
                order { id totalPriceSet { shopMoney { amount } } }
                userErrors { message }
              }
            }
            """,
            {"id": calc_id},
        )
        if not commit:
            return False, "Could not save order changes"
        block = commit.get("orderEditCommit") or {}
        if block.get("userErrors"):
            return False, block["userErrors"][0].get("message", "commit failed")
        return True, None

    def _pick_calc_line_node(
        self,
        nodes: list[dict[str, Any]],
        raw_line: dict[str, Any],
        *,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any] | None:
        if not nodes:
            return None
        raw_title = str(raw_line.get("title") or "").lower()
        raw_variant = str(raw_line.get("variant_title") or "").lower()
        for node in nodes:
            title = str(node.get("title") or "").lower()
            variant = str(node.get("variantTitle") or "").lower()
            if title == raw_title and (not raw_variant or variant == raw_variant):
                return node
        needle = (product_name or "").strip().lower()
        if needle:
            for node in nodes:
                title = str(node.get("title") or "").lower()
                variant = str(node.get("variantTitle") or "").lower()
                if needle in title or needle in variant:
                    return node
        idx = max(0, min(line_index, len(nodes) - 1))
        return nodes[idx]

    def _editable_order_guard(self, raw: dict[str, Any] | None, order_id: str) -> dict[str, Any] | None:
        if not raw:
            return {"success": False, "message": "Order not found"}
        if raw.get("cancelled_at"):
            return {"success": False, "message": "Order is already cancelled"}
        if not self.is_unfulfilled(raw):
            return {
                "success": False,
                "message": "Order is already packed or shipped — changes must be done via support ticket",
            }
        return None

    async def update_shipping_address(
        self,
        order_id: str,
        *,
        address1: str,
        city: str,
        zip_code: str,
        province: str = "",
        address2: str = "",
        name: str = "",
        phone: str = "",
        country: str = "India",
    ) -> dict[str, Any]:
        """Update delivery address on an unfulfilled order (REST)."""
        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked

        shopify_id = int(raw["id"])
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        existing = dict(raw.get("shipping_address") or {})
        shipping = {
            "first_name": existing.get("first_name") or (name.split()[0] if name else "Customer"),
            "last_name": existing.get("last_name")
            or (name.split()[-1] if name and len(name.split()) > 1 else ""),
            "name": name or existing.get("name") or "",
            "address1": address1.strip(),
            "address2": (address2 or "").strip(),
            "city": city.strip(),
            "province": (province or existing.get("province") or "").strip(),
            "zip": zip_code.strip(),
            "country": country or existing.get("country") or "India",
            "phone": phone.strip() or existing.get("phone") or raw.get("phone") or "",
        }
        payload = {"order": {"id": shopify_id, "shipping_address": shipping}}
        result = await self._request("PUT", f"/orders/{shopify_id}.json", json=payload)
        if result is None:
            return {"success": False, "message": "Shopify could not update the address"}

        self._invalidate_order_cache(order_id)
        updated = (result.get("order") or {}).get("shipping_address") or shipping
        formatted = format_delivery_address(updated)
        logger.info("Shopify address updated order=%s", display_id)
        return {
            "success": True,
            "order_id": display_id,
            "delivery_address": formatted,
            "message": "Delivery address updated successfully",
        }

    async def update_line_item_quantity(
        self,
        order_id: str,
        *,
        quantity: int,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any]:
        """Change quantity on an unfulfilled order (GraphQL order edit)."""
        if quantity < 1:
            return {"success": False, "message": "Quantity must be at least 1"}

        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked

        shopify_id = int(raw["id"])
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        order_gid = f"gid://shopify/Order/{shopify_id}"

        begin = await self._graphql(
            """
            mutation orderEditBegin($id: ID!) {
              orderEditBegin(id: $id) {
                calculatedOrder {
                  id
                  lineItems(first: 25) {
                    edges {
                      node { id quantity title variantTitle }
                    }
                  }
                }
                userErrors { message }
              }
            }
            """,
            {"id": order_gid},
        )
        if not begin:
            return {"success": False, "message": "Could not start order edit in Shopify"}

        calc = (begin.get("orderEditBegin") or {}).get("calculatedOrder")
        if not calc:
            errors = (begin.get("orderEditBegin") or {}).get("userErrors") or []
            msg = errors[0].get("message") if errors else "orderEditBegin failed"
            return {"success": False, "message": msg}

        calc_id = calc["id"]
        edges = (calc.get("lineItems") or {}).get("edges") or []
        nodes = [e.get("node") for e in edges if e.get("node")]
        if not nodes:
            return {"success": False, "message": "No line items on this order"}

        target = None
        needle = (product_name or "").strip().lower()
        if needle:
            for node in nodes:
                title = str(node.get("title") or "").lower()
                variant = str(node.get("variantTitle") or "").lower()
                if needle in title or needle in variant:
                    target = node
                    break
        if not target:
            idx = max(0, min(line_index, len(nodes) - 1))
            target = nodes[idx]

        set_qty = await self._graphql(
            """
            mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
              orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
                calculatedOrder { id }
                userErrors { message }
              }
            }
            """,
            {
                "id": calc_id,
                "lineItemId": target["id"],
                "quantity": int(quantity),
            },
        )
        if not set_qty:
            return {"success": False, "message": "Could not set quantity in Shopify"}

        set_block = set_qty.get("orderEditSetQuantity") or {}
        if set_block.get("userErrors"):
            return {
                "success": False,
                "message": set_block["userErrors"][0].get("message", "setQuantity failed"),
            }

        commit = await self._graphql(
            """
            mutation orderEditCommit($id: ID!) {
              orderEditCommit(id: $id) {
                order { id name totalPriceSet { shopMoney { amount currencyCode } } }
                userErrors { message }
              }
            }
            """,
            {"id": calc_id},
        )
        if not commit:
            return {"success": False, "message": "Could not save quantity change"}

        commit_block = commit.get("orderEditCommit") or {}
        if commit_block.get("userErrors"):
            return {
                "success": False,
                "message": commit_block["userErrors"][0].get("message", "commit failed"),
            }

        self._invalidate_order_cache(order_id)
        refreshed = await self._find_raw_order(order_id)
        new_total = float((refreshed or {}).get("total_price") or 0)
        logger.info(
            "Shopify quantity updated order=%s item=%s qty=%s",
            display_id,
            target.get("title"),
            quantity,
        )
        return {
            "success": True,
            "order_id": display_id,
            "product": target.get("title"),
            "quantity": quantity,
            "total_inr": new_total,
            "message": f"Updated {target.get('title')} to quantity {quantity}",
        }

    async def list_item_variants(
        self,
        order_id: str,
        *,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any]:
        """List size/colour variants in stock for a line item on this order."""
        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked

        raw_line = self._pick_raw_line_item(raw, product_name=product_name, line_index=line_index)
        if not raw_line:
            return {"success": False, "message": "Line item not found on order"}

        product_id = raw_line.get("product_id")
        if not product_id:
            return {"success": False, "message": "Product ID missing — cannot list variants"}

        variants = await self._fetch_product_variants(int(product_id))
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        current_vid = raw_line.get("variant_id")
        available = [v for v in variants if v.get("in_stock")]

        return {
            "success": True,
            "order_id": display_id,
            "product": raw_line.get("title"),
            "current_variant": raw_line.get("variant_title"),
            "current_variant_id": current_vid,
            "variants": [
                {
                    "variant_id": v["variant_id"],
                    "label": v["display"],
                    "price_inr": v["price"],
                    "in_stock": v["in_stock"],
                    "is_current": v["variant_id"] == current_vid,
                }
                for v in variants
            ],
            "in_stock_count": len(available),
            "message": (
                f"{len(available)} variant(s) in stock"
                if available
                else "No variants currently in stock"
            ),
        }

    async def swap_line_item_variant(
        self,
        order_id: str,
        *,
        new_variant: str,
        product_name: str = "",
        line_index: int = 0,
        quantity: int | None = None,
    ) -> dict[str, Any]:
        """Swap size/colour/variant on an unfulfilled order (remove old + add new)."""
        choice = (new_variant or "").strip()
        if not choice:
            return {"success": False, "message": "New size or colour is required"}

        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked

        raw_line = self._pick_raw_line_item(raw, product_name=product_name, line_index=line_index)
        if not raw_line:
            return {"success": False, "message": "Line item not found on order"}

        product_id = raw_line.get("product_id")
        if not product_id:
            return {"success": False, "message": "Product ID missing — cannot swap variant"}

        variants = await self._fetch_product_variants(int(product_id))
        matched = self._match_product_variant(variants, choice)
        if not matched:
            labels = ", ".join(
                str(v.get("display") or v.get("title") or "")
                for v in variants
                if v.get("display") or v.get("title")
            )[:200]
            return {
                "success": False,
                "message": f"Could not find variant matching '{choice}'. Available: {labels}",
            }

        if not matched.get("in_stock"):
            return {
                "success": False,
                "message": (
                    f"'{matched.get('display')}' is out of stock. "
                    "Offer another size or colour."
                ),
            }

        current_vid = raw_line.get("variant_id")
        if current_vid and int(current_vid) == int(matched["variant_id"]):
            return {
                "success": True,
                "message": "Order already has that variant",
                "variant": matched.get("display"),
            }

        qty = int(quantity) if quantity is not None else int(raw_line.get("quantity") or 1)
        if qty < 1:
            return {"success": False, "message": "Quantity must be at least 1"}

        shopify_id = int(raw["id"])
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        calc_id, nodes, err = await self._order_edit_begin(shopify_id)
        if err:
            return {"success": False, "message": err}

        calc_line = self._pick_calc_line_node(
            nodes, raw_line, product_name=product_name, line_index=line_index
        )
        if not calc_line:
            return {"success": False, "message": "Could not match line item in order edit"}

        remove = await self._graphql(
            """
            mutation orderEditSetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
              orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity) {
                calculatedOrder { id }
                userErrors { message }
              }
            }
            """,
            {"id": calc_id, "lineItemId": calc_line["id"], "quantity": 0},
        )
        if not remove:
            return {"success": False, "message": "Could not remove old variant from order"}
        rem_err = (remove.get("orderEditSetQuantity") or {}).get("userErrors") or []
        if rem_err:
            return {"success": False, "message": rem_err[0].get("message", "remove failed")}

        variant_gid = f"gid://shopify/ProductVariant/{matched['variant_id']}"
        add = await self._graphql(
            """
            mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
              orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
                calculatedOrder { id }
                userErrors { message }
              }
            }
            """,
            {"id": calc_id, "variantId": variant_gid, "quantity": qty},
        )
        if not add:
            return {"success": False, "message": "Could not add new variant to order"}
        add_err = (add.get("orderEditAddVariant") or {}).get("userErrors") or []
        if add_err:
            return {"success": False, "message": add_err[0].get("message", "add variant failed")}

        ok, commit_err = await self._order_edit_commit(calc_id)
        if not ok:
            return {"success": False, "message": commit_err or "commit failed"}

        self._invalidate_order_cache(order_id)
        refreshed = await self._find_raw_order(order_id)
        new_total = float((refreshed or {}).get("total_price") or 0)
        new_label = matched.get("display") or choice
        logger.info(
            "Shopify variant swapped order=%s product=%s -> %s",
            display_id,
            raw_line.get("title"),
            new_label,
        )
        return {
            "success": True,
            "order_id": display_id,
            "product": raw_line.get("title"),
            "old_variant": raw_line.get("variant_title"),
            "new_variant": new_label,
            "quantity": qty,
            "total_inr": new_total,
            "price_inr": matched.get("price"),
            "message": f"Swapped to {new_label} (qty {qty})",
        }

    async def get_confirmation_order_snapshot(self, order_id: str) -> dict[str, Any]:
        """Fresh order facts for outbound confirmation (read-only)."""
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"found": False, "message": "Order not found"}
        order = order_from_shopify(raw)
        display_id = str(raw.get("name", order_id)).lstrip("#").upper()
        lines = self.line_items_from_raw(raw)
        return {
            "found": True,
            "order_id": display_id,
            "customer_name": order.customer_name,
            "items": [li.spoken_summary() for li in lines],
            "line_items": [
                {
                    "line_index": i,
                    "title": li.title,
                    "quantity": li.quantity,
                    "variant": li.variant_title,
                }
                for i, li in enumerate(lines)
            ],
            "total_inr": order.total_inr,
            "status": order.status,
            "delivery_address": order.delivery_address,
            "editable": self.is_unfulfilled(raw) and not raw.get("cancelled_at"),
        }

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
