"""Magento 2.4 Admin REST API — live orders for voice support (separate from Shopify)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.database.mock_db import Order
from app.models.order_line_item import OrderLineItemSnapshot
from app.services.shopify_tags import order_should_skip_auto_call
from app.utils.address import format_delivery_address, shipping_address_is_complete
from app.utils.config import Settings

logger = logging.getLogger(__name__)

# Status-history comment prefixes (Magento has no order tags).
AI_MARKER_PREFIX = "[AI]"


def parse_magento_store_codes(settings: Settings) -> list[str]:
    """
    REST store view codes for /rest/{code}/V1.

    Use MAGENTO_STORE_CODES for multi-store (comma-separated). Otherwise
    MAGENTO_STORE_CODE (``default``, ``all``, or one view code).
    """
    multi = (settings.magento_store_codes or "").strip()
    if multi:
        return [c.strip() for c in multi.split(",") if c.strip()]
    single = (settings.magento_store_code or "default").strip() or "default"
    # Allow MAGENTO_STORE_CODE=thamesmead,ades (prefer MAGENTO_STORE_CODES for clarity).
    if "," in single:
        return [c.strip() for c in single.split(",") if c.strip()]
    return [single]


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


def _parse_order_number(order_id: str) -> str:
    raw = order_id.strip()
    if raw.upper().startswith("ORD-"):
        return raw[4:].strip()
    return re.sub(r"[^0-9A-Za-z-]", "", raw) or raw


def _magento_address_dict(addr: dict[str, Any] | None) -> dict[str, Any]:
    if not addr:
        return {}
    street = addr.get("street")
    if isinstance(street, list):
        address1 = str(street[0]).strip() if street else ""
        address2 = str(street[1]).strip() if len(street) > 1 else ""
    else:
        address1 = str(street or "").strip()
        address2 = ""
    first = str(addr.get("firstname") or "").strip()
    last = str(addr.get("lastname") or "").strip()
    name = f"{first} {last}".strip() or str(addr.get("company") or "").strip()
    return {
        "name": name,
        "address1": address1,
        "address2": address2,
        "city": str(addr.get("city") or "").strip(),
        "province": str(addr.get("region") or addr.get("region_code") or "").strip(),
        "zip": str(addr.get("postcode") or "").strip(),
        "country": str(addr.get("country_id") or addr.get("country") or "").strip(),
        "phone": str(addr.get("telephone") or "").strip(),
    }


def _shipping_address_from_order(raw: dict[str, Any]) -> dict[str, Any]:
    ext = raw.get("extension_attributes") or {}
    assignments = ext.get("shipping_assignments") or []
    if assignments:
        ship = (assignments[0].get("shipping") or {}).get("address")
        if isinstance(ship, dict):
            return _magento_address_dict(ship)
    billing = raw.get("billing_address")
    if isinstance(billing, dict):
        return _magento_address_dict(billing)
    return {}


def _map_magento_status(raw: dict[str, Any]) -> str:
    status = (raw.get("status") or "").lower()
    state = (raw.get("state") or "").lower()
    if status in ("canceled", "cancelled") or state == "canceled":
        return "cancelled"
    if status in ("closed", "complete") or state in ("closed", "complete"):
        return "delivered"
    if status in ("holded", "hold"):
        return "processing"
    return status or state or "processing"


def _refund_status_from_order(raw: dict[str, Any]) -> str:
    total_refunded = float(raw.get("total_refunded") or 0)
    grand = float(raw.get("grand_total") or 0)
    if total_refunded <= 0:
        return "not_requested"
    if grand > 0 and total_refunded >= grand * 0.99:
        return "completed"
    return "pending"


def order_from_magento(raw: dict[str, Any]) -> Order:
    ship = _shipping_address_from_order(raw)
    billing = _magento_address_dict(raw.get("billing_address"))
    delivery_address = format_delivery_address(ship) or format_delivery_address(billing)
    customer = raw.get("customer") if isinstance(raw.get("customer"), dict) else {}
    first = str(customer.get("firstname") or "").strip()
    last = str(customer.get("lastname") or "").strip()
    name = f"{first} {last}".strip() or str(ship.get("name") or "").strip() or "Customer"

    items = [
        str(li.get("name") or li.get("sku") or "Item")
        for li in (raw.get("items") or [])
        if isinstance(li, dict)
    ]

    created = raw.get("created_at") or ""
    try:
        placed_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        placed_at = datetime.now(timezone.utc)

    currency = raw.get("order_currency_code") or raw.get("base_currency_code") or "INR"
    total = float(raw.get("grand_total") or raw.get("base_grand_total") or 0)
    status = _map_magento_status(raw)

    return Order(
        order_id=str(raw.get("increment_id") or raw.get("entity_id", "")).upper(),
        customer_name=name,
        phone=MagentoRepository.extract_phone(raw),
        status=status,
        total_inr=total if currency == "INR" else total,
        items=items,
        placed_at=placed_at,
        tracking_id=None,
        carrier=None,
        refund_status=_refund_status_from_order(raw),
        cancel_allowed=status not in ("cancelled", "delivered", "refunded"),
        delivery_address=delivery_address,
    )


class MagentoRepository:
    """Read/write orders via Magento 2 REST API (integration token)."""

    def __init__(self, settings: Settings) -> None:
        base = settings.magento_base_url.strip().rstrip("/")
        if not base:
            raise ValueError("MAGENTO_BASE_URL is required when DATA_SOURCE=magento")
        self._token = settings.magento_access_token.strip()
        if not self._token:
            raise ValueError("MAGENTO_ACCESS_TOKEN is required when DATA_SOURCE=magento")
        self._root_base = base
        self._store_codes = parse_magento_store_codes(settings)
        self._timeout = settings.store_api_timeout
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._order_cache: dict[str, dict[str, Any]] = {}
        self._order_store_code: dict[str, str] = {}

    def _api_base(self, store_code: str) -> str:
        return f"{self._root_base}/rest/{store_code}/V1"

    def _cache_order(self, key: str, raw: dict[str, Any], store_code: str) -> None:
        self._order_cache[key] = raw
        self._order_store_code[key] = store_code

    def _store_for_order_key(self, key: str) -> str:
        return self._order_store_code.get(key) or self._store_codes[0]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        store_code: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self._api_base(store_code)}{path}"
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
                data = response.json()
                return data if isinstance(data, dict) else {"items": data}
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = (exc.response.text or "")[:500]
            except Exception:
                pass
            logger.error(
                "Magento API %s %s → %s %s",
                method,
                path,
                exc.response.status_code,
                body,
            )
            return None
        except httpx.HTTPError as exc:
            logger.error("Magento API %s %s failed: %s", method, path, exc)
            return None

    def _cache_key(self, order_id: str) -> str:
        return _parse_order_number(order_id).upper()

    def _invalidate_order_cache(self, order_id: str) -> None:
        key = self._cache_key(order_id)
        self._order_cache.pop(key, None)
        self._order_store_code.pop(key, None)

    @staticmethod
    def marker_comment(tag: str) -> str:
        return f"{AI_MARKER_PREFIX} {tag.strip()}"

    @staticmethod
    def parse_markers_from_histories(raw: dict[str, Any]) -> list[str]:
        tags: list[str] = []
        for entry in raw.get("status_histories") or []:
            if not isinstance(entry, dict):
                continue
            comment = str(entry.get("comment") or "")
            if not comment.startswith(AI_MARKER_PREFIX):
                continue
            label = comment[len(AI_MARKER_PREFIX) :].strip()
            if label:
                tags.append(label)
        return tags

    @staticmethod
    def markers_as_set(raw: dict[str, Any]) -> set[str]:
        return {t.strip().lower() for t in MagentoRepository.parse_markers_from_histories(raw)}

    @staticmethod
    def extract_phone(raw: dict[str, Any]) -> str:
        if raw.get("telephone") and str(raw["telephone"]).strip():
            return str(raw["telephone"]).strip()
        ship = _shipping_address_from_order(raw)
        if ship.get("phone"):
            return str(ship["phone"]).strip()
        billing = _magento_address_dict(raw.get("billing_address"))
        if billing.get("phone"):
            return str(billing["phone"]).strip()
        return ""

    @staticmethod
    def phone_missing_reason(raw: dict[str, Any]) -> str:
        if MagentoRepository.extract_phone(raw):
            return ""
        return (
            'No phone in Magento API response. Pass "phone" in '
            "POST /api/outbound/confirm-order or enable order desk import."
        )

    @staticmethod
    def is_unfulfilled(raw: dict[str, Any]) -> bool:
        status = (raw.get("status") or "").lower()
        state = (raw.get("state") or "").lower()
        if status in ("canceled", "cancelled", "closed", "complete") or state in (
            "canceled",
            "closed",
            "complete",
        ):
            return False
        return True

    def order_needs_confirmation(self, raw: dict[str, Any]) -> tuple[bool, str]:
        if not self.is_unfulfilled(raw):
            return False, "Order is not open for confirmation"
        markers = self.markers_as_set(raw)
        if order_should_skip_auto_call(markers):
            return False, f"Order already confirmed or in progress: {', '.join(sorted(markers))}"
        return True, "needs_confirmation"

    def is_eligible_for_confirmation_call(self, raw: dict[str, Any]) -> tuple[bool, str]:
        needs, reason = self.order_needs_confirmation(raw)
        if not needs:
            return False, reason
        if not self.extract_phone(raw):
            return False, self.phone_missing_reason(raw)
        return True, "eligible"

    async def _probe_store_scope(self, store_code: str) -> tuple[bool, str]:
        """Health-check one store view — orders API (storeConfigs often 400s on cloud)."""
        params = {
            "searchCriteria[pageSize]": "1",
            "searchCriteria[currentPage]": "1",
        }
        data = await self._request(
            "GET", "/orders", store_code=store_code, params=params
        )
        if data is not None:
            return True, "orders"
        configs = await self._request(
            "GET", "/store/storeConfigs", store_code=store_code
        )
        if configs is not None:
            return True, "storeConfigs"
        return False, "orders and storeConfigs failed"

    async def verify_connection(self) -> tuple[bool, str]:
        ok_codes: list[str] = []
        failed: list[str] = []
        for code in self._store_codes:
            ok, _via = await self._probe_store_scope(code)
            if ok:
                ok_codes.append(code)
            else:
                failed.append(code)
        if not ok_codes:
            hint = ""
            if failed:
                hint = f" Failed store view code(s): {', '.join(failed)}."
            return (
                False,
                "Could not reach Magento REST API. Check MAGENTO_BASE_URL, "
                "MAGENTO_STORE_CODES (comma-separated store *view* codes), "
                f"and MAGENTO_ACCESS_TOKEN.{hint} "
                "Try MAGENTO_STORE_CODE=all or fix codes in Admin → Stores → All Stores.",
            )
        extra = f" (failed: {', '.join(failed)})" if failed else ""
        return (
            True,
            f"Connected to Magento REST ({len(ok_codes)} scope(s): "
            f"{', '.join(ok_codes)}){extra}",
        )

    async def _find_by_increment_id(
        self, increment_id: str, store_code: str
    ) -> dict[str, Any] | None:
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "increment_id",
            "searchCriteria[filterGroups][0][filters][0][value]": increment_id,
            "searchCriteria[filterGroups][0][filters][0][conditionType]": "eq",
            "searchCriteria[pageSize]": "5",
        }
        data = await self._request("GET", "/orders", params=params, store_code=store_code)
        items = (data or {}).get("items") or []
        for item in items:
            if str(item.get("increment_id", "")).strip() == increment_id:
                return item
        return items[0] if items else None

    async def _find_raw_order_in_store(
        self,
        order_id: str,
        store_code: str,
    ) -> dict[str, Any] | None:
        needle = _parse_order_number(order_id)
        if not needle:
            return None

        if needle.isdigit():
            entity = await self._request(
                "GET", f"/orders/{needle}", store_code=store_code
            )
            if entity and entity.get("entity_id"):
                return entity

        found = await self._find_by_increment_id(needle, store_code)
        if found:
            return found

        if needle.isdigit():
            padded = needle.zfill(9)
            return await self._find_by_increment_id(padded, store_code)
        return None

    async def _find_raw_order(
        self,
        order_id: str,
        *,
        store_view_code: str | None = None,
    ) -> dict[str, Any] | None:
        key = self._cache_key(order_id)
        if key in self._order_cache:
            return self._order_cache[key]

        codes: list[str]
        hint = (store_view_code or "").strip()
        if hint:
            codes = [hint] if hint in self._store_codes else [hint, *self._store_codes]
        else:
            codes = list(self._store_codes)

        seen: set[str] = set()
        for code in codes:
            if code in seen:
                continue
            seen.add(code)
            found = await self._find_raw_order_in_store(order_id, code)
            if found:
                self._cache_order(key, found, code)
                return found
        return None

    async def get_raw_order(
        self,
        order_id: str,
        *,
        store_view_code: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._find_raw_order(order_id, store_view_code=store_view_code)

    async def get_raw_order_by_entity_id(
        self,
        entity_id: int | str,
        *,
        store_view_code: str | None = None,
    ) -> dict[str, Any] | None:
        codes: list[str] = (
            [store_view_code.strip()]
            if store_view_code and store_view_code.strip()
            else list(self._store_codes)
        )
        seen: set[str] = set()
        for code in codes:
            if not code or code in seen:
                continue
            seen.add(code)
            data = await self._request(
                "GET", f"/orders/{entity_id}", store_code=code
            )
            if data and data.get("entity_id"):
                key = str(data.get("increment_id", entity_id)).upper()
                self._cache_order(key, data, code)
                return data
        return None

    async def append_marker(self, order_id: str, marker: str) -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"success": False, "message": "Order not found"}
        entity_id = int(raw["entity_id"])
        display_id = str(raw.get("increment_id", order_id)).upper()
        body = {
            "statusHistory": {
                "comment": self.marker_comment(marker),
                "status": raw.get("status") or "processing",
                "is_customer_notified": 0,
                "is_visible_on_front": 0,
            }
        }
        store_code = self._store_for_order_key(self._cache_key(order_id))
        result = await self._request(
            "POST",
            f"/orders/{entity_id}/comments",
            store_code=store_code,
            json=body,
        )
        if result is None:
            return {"success": False, "message": "Magento could not add status comment"}
        self._invalidate_order_cache(order_id)
        return {"success": True, "order_id": display_id, "marker": marker}

    async def reconcile_markers(
        self,
        order_id: str,
        *,
        add_markers: list[str],
        remove_markers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Append AI markers (Magento has no tag delete — new comments only)."""
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"success": False, "message": "Order not found"}
        display_id = str(raw.get("increment_id", order_id)).upper()
        existing = {m.lower() for m in self.parse_markers_from_histories(raw)}
        added: list[str] = []
        for marker in add_markers:
            label = marker.strip()
            if not label or label.lower() in existing:
                continue
            res = await self.append_marker(order_id, label)
            if res.get("success"):
                added.append(label)
                existing.add(label.lower())
        return {
            "success": True,
            "order_id": display_id,
            "added": added,
            "removed": list(remove_markers or []),
            "message": "Markers appended via order status history",
        }

    async def append_order_note(self, order_id: str, note_line: str) -> dict[str, Any]:
        line = (note_line or "").strip()
        if not line:
            return {"success": False, "message": "Note line is empty"}
        return await self.append_marker(order_id, line[:200])

    async def list_recent_unfulfilled_orders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, str] = {
            "searchCriteria[sortOrders][0][field]": "created_at",
            "searchCriteria[sortOrders][0][direction]": "DESC",
            "searchCriteria[pageSize]": str(min(limit, 100)),
            "searchCriteria[currentPage]": "1",
        }
        idx = 0
        for status in ("pending", "processing", "payment_review"):
            params[f"searchCriteria[filterGroups][0][filters][{idx}][field]"] = "status"
            params[f"searchCriteria[filterGroups][0][filters][{idx}][value]"] = status
            params[f"searchCriteria[filterGroups][0][filters][{idx}][conditionType]"] = "eq"
            idx += 1
        merged: list[dict[str, Any]] = []
        seen_entity: set[int] = set()
        per_store = max(10, limit // max(len(self._store_codes), 1))
        for code in self._store_codes:
            p = dict(params)
            p["searchCriteria[pageSize]"] = str(min(per_store, 100))
            data = await self._request("GET", "/orders", params=p, store_code=code)
            for o in (data or {}).get("items") or []:
                eid = o.get("entity_id")
                if eid is not None and int(eid) in seen_entity:
                    continue
                if eid is not None:
                    seen_entity.add(int(eid))
                if self.is_unfulfilled(o):
                    merged.append(o)
        merged.sort(
            key=lambda o: str(o.get("created_at") or ""),
            reverse=True,
        )
        return merged[:limit]

    async def get_order(self, order_id: str) -> Order | None:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return None
        order = order_from_magento(raw)
        order.order_id = str(raw.get("increment_id", order_id)).upper()
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
            return {"found": False, "message": f"No order found with ID {order_id.upper()}."}
        status = _refund_status_from_order(raw)
        display_id = str(raw.get("increment_id", order_id)).upper()
        messages = {
            "not_requested": "No refund has been requested for this order.",
            "pending": f"Refund is being processed and should reflect by {_default_eta_date_str()}.",
            "completed": "Refund has been completed to the original payment method.",
        }
        return {
            "found": True,
            "order_id": display_id,
            "order_status": _map_magento_status(raw),
            "refund_status": status,
            "message": messages.get(status, f"Refund status: {status}"),
        }

    async def _tracking_from_shipments(
        self, entity_id: int, store_code: str
    ) -> tuple[str | None, str | None]:
        params = {
            "searchCriteria[filterGroups][0][filters][0][field]": "order_id",
            "searchCriteria[filterGroups][0][filters][0][value]": str(entity_id),
            "searchCriteria[filterGroups][0][filters][0][conditionType]": "eq",
            "searchCriteria[pageSize]": "5",
        }
        data = await self._request(
            "GET", "/shipments", params=params, store_code=store_code
        )
        for shipment in (data or {}).get("items") or []:
            tracks = shipment.get("tracks") or []
            if tracks and isinstance(tracks[0], dict):
                tn = tracks[0].get("track_number") or tracks[0].get("number")
                carrier = tracks[0].get("title") or tracks[0].get("carrier_code")
                if tn:
                    return str(tn), str(carrier) if carrier else None
            if shipment.get("increment_id"):
                return str(shipment["increment_id"]), "Shipment"
        return None, None

    async def get_tracking(self, order_id: str) -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"found": False, "message": "Order not found."}

        display_id = str(raw.get("increment_id", order_id)).upper()
        status = _map_magento_status(raw)
        ship = _shipping_address_from_order(raw)
        delivery_address = format_delivery_address(ship)
        shipping_phone = self.extract_phone(raw)

        key = self._cache_key(order_id)
        store_code = self._store_for_order_key(key)
        tracking_id, carrier = await self._tracking_from_shipments(
            int(raw["entity_id"]), store_code
        )

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
            if shipping_phone:
                payload["shipping_phone"] = shipping_phone
            return payload

        payload = {
            "found": True,
            "order_id": display_id,
            "status": status,
            "tracking_available": True,
            "tracking_id": tracking_id,
            "carrier": carrier or "Courier Partner",
            "estimated_delivery": _default_eta_date_str(),
        }
        if delivery_address:
            payload["delivery_address"] = delivery_address
        if shipping_phone:
            payload["shipping_phone"] = shipping_phone
        return payload

    @staticmethod
    def line_items_from_raw(raw: dict[str, Any]) -> list[OrderLineItemSnapshot]:
        snapshots: list[OrderLineItemSnapshot] = []
        for li in raw.get("items") or []:
            if not isinstance(li, dict):
                continue
            lid = li.get("item_id")
            if lid is None:
                continue
            snapshots.append(
                OrderLineItemSnapshot(
                    line_item_id=int(lid),
                    title=str(li.get("name") or "Item"),
                    quantity=int(li.get("qty_ordered") or li.get("qty") or 1),
                    variant_title=None,
                    sku=(li.get("sku") or None),
                    product_id=int(li["product_id"]) if li.get("product_id") else None,
                    variant_id=None,
                )
            )
        return snapshots

    async def get_confirmation_order_snapshot(self, order_id: str) -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"found": False, "message": "Order not found"}
        order = order_from_magento(raw)
        display_id = str(raw.get("increment_id", order_id)).upper()
        lines = self.line_items_from_raw(raw)
        ship = _shipping_address_from_order(raw)
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
            "shipping_phone": order.phone or self.extract_phone(raw),
            "address_source": "shipping_address",
            "editable": self.is_unfulfilled(raw),
            "data_source": "magento",
        }

    async def cancel_order_api(self, order_id: str, reason: str = "") -> dict[str, Any]:
        raw = await self._find_raw_order(order_id)
        if not raw:
            return {"found": False, "success": False, "message": "Order not found."}

        display_id = str(raw.get("increment_id", order_id)).upper()
        status = _map_magento_status(raw)
        if status == "cancelled":
            return {
                "found": True,
                "success": False,
                "message": "Order is already cancelled.",
                "order_id": display_id,
            }
        if status == "delivered":
            return {
                "found": True,
                "success": False,
                "message": "Completed orders cannot be cancelled via phone support.",
                "order_id": display_id,
            }

        entity_id = int(raw["entity_id"])
        store_code = self._store_for_order_key(self._cache_key(order_id))
        result = await self._request(
            "POST",
            f"/orders/{entity_id}/cancel",
            store_code=store_code,
        )
        if result is None:
            return {
                "found": True,
                "success": False,
                "message": "Magento could not cancel the order.",
                "order_id": display_id,
            }

        if reason:
            await self.append_order_note(order_id, f"Voice cancel: {reason[:300]}")

        self._invalidate_order_cache(order_id)
        return {
            "found": True,
            "success": True,
            "order_id": display_id,
            "message": "Order cancelled successfully in Magento.",
            "reason": reason or "Customer request via voice support",
        }

    _ARIA_EDIT_PREFIX = "/aria-voice"

    @staticmethod
    def _raw_shipping_address_record(raw: dict[str, Any]) -> dict[str, Any] | None:
        ext = raw.get("extension_attributes") or {}
        assignments = ext.get("shipping_assignments") or []
        if assignments:
            ship = (assignments[0].get("shipping") or {}).get("address")
            if isinstance(ship, dict):
                return ship
        return None

    def _editable_order_guard(
        self, raw: dict[str, Any] | None, order_id: str
    ) -> dict[str, Any] | None:
        if not raw:
            return {"success": False, "message": "Order not found"}
        status = _map_magento_status(raw)
        if status == "cancelled":
            return {"success": False, "message": "Order is already cancelled"}
        if not self.is_unfulfilled(raw):
            return {
                "success": False,
                "message": (
                    "Order is already complete or shipped — "
                    "changes must be done via support"
                ),
            }
        return None

    async def _request_edit(
        self,
        method: str,
        path: str,
        *,
        store_code: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call Aria_VoiceOrder REST routes; body is a JSON string from Magento."""
        url = f"{self._api_base(store_code)}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method, url, headers=self._headers, json=json_body
                )
                if response.status_code == 404:
                    return {
                        "success": False,
                        "message": (
                            "Magento order-edit API not found. Install the Aria_VoiceOrder "
                            "module (see magento_module/README.md), enable it, grant the "
                            "integration Sales → Aria voice order edits, then flush cache."
                        ),
                    }
                response.raise_for_status()
                if not response.content:
                    return {"success": True}
                data = response.json()
                if isinstance(data, str):
                    try:
                        parsed = json.loads(data)
                        return parsed if isinstance(parsed, dict) else {"success": True}
                    except json.JSONDecodeError:
                        return {"success": True, "message": data}
                if isinstance(data, dict):
                    return data
                return {"success": True}
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = (exc.response.text or "")[:500]
            except Exception:
                pass
            logger.error(
                "Magento edit API %s %s → %s %s",
                method,
                path,
                exc.response.status_code,
                body,
            )
            return {
                "success": False,
                "message": body or f"Magento API error {exc.response.status_code}",
            }
        except httpx.HTTPError as exc:
            logger.error("Magento edit API %s %s failed: %s", method, path, exc)
            return {"success": False, "message": str(exc)}

    def _pick_line_item(
        self,
        raw: dict[str, Any],
        *,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any] | None:
        items = [
            li for li in (raw.get("items") or []) if isinstance(li, dict) and li.get("item_id")
        ]
        if not items:
            return None
        needle = (product_name or "").strip().lower()
        if needle:
            for li in items:
                title = str(li.get("name") or li.get("sku") or "").lower()
                if needle in title:
                    return li
        idx = max(0, min(line_index, len(items) - 1))
        return items[idx]

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
        country: str = "GB",
    ) -> dict[str, Any]:
        """Update shipping address via Aria_VoiceOrder module (requires install on Magento)."""
        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked
        assert raw is not None

        entity_id = int(raw["entity_id"])
        display_id = str(raw.get("increment_id", order_id)).upper()
        store_code = self._store_for_order_key(self._cache_key(order_id))

        body = {
            "address1": address1.strip(),
            "city": city.strip(),
            "postcode": zip_code.strip(),
            "address2": (address2 or "").strip(),
            "province": (province or "").strip(),
            "name": (name or "").strip(),
            "phone": (phone or "").strip(),
            "countryId": (country or "GB").strip() or "GB",
        }
        result = await self._request_edit(
            "PUT",
            f"{self._ARIA_EDIT_PREFIX}/orders/{entity_id}/shipping-address",
            store_code=store_code,
            json_body=body,
        )
        if not result.get("success"):
            return result

        self._invalidate_order_cache(order_id)
        delivery = str(result.get("delivery_address") or "").strip()
        if not delivery:
            refreshed = await self._find_raw_order(order_id)
            if refreshed:
                ship = _shipping_address_from_order(refreshed)
                delivery = format_delivery_address(ship)
        if delivery:
            result["delivery_address"] = delivery
        result.setdefault("order_id", display_id)
        result.setdefault("message", "Delivery address updated successfully in Magento.")
        result["magento_sync"] = "ok"
        logger.info("Magento address updated order=%s", display_id)
        return result

    async def update_line_item_quantity(
        self,
        order_id: str,
        *,
        quantity: int,
        product_name: str = "",
        line_index: int = 0,
    ) -> dict[str, Any]:
        """Change line qty via Aria_VoiceOrder module."""
        if quantity < 1:
            return {"success": False, "message": "Quantity must be at least 1"}

        raw = await self._find_raw_order(order_id)
        blocked = self._editable_order_guard(raw, order_id)
        if blocked:
            return blocked
        assert raw is not None

        line = self._pick_line_item(
            raw, product_name=product_name, line_index=line_index
        )
        if not line:
            return {"success": False, "message": "No matching line item on this order"}

        item_id = int(line["item_id"])
        display_id = str(raw.get("increment_id", order_id)).upper()
        store_code = self._store_for_order_key(self._cache_key(order_id))

        result = await self._request_edit(
            "PUT",
            f"{self._ARIA_EDIT_PREFIX}/orders/items/{item_id}/quantity",
            store_code=store_code,
            json_body={"qty": float(quantity)},
        )
        if not result.get("success"):
            return result

        self._invalidate_order_cache(order_id)
        refreshed = await self._find_raw_order(order_id)
        total = float(result.get("total") or 0)
        if refreshed:
            total = float(refreshed.get("grand_total") or refreshed.get("base_grand_total") or total)

        result["order_id"] = display_id
        result["total_inr"] = total
        result["magento_sync"] = "ok"
        result.setdefault("message", "Line item quantity updated in Magento.")
        logger.info("Magento qty updated order=%s item=%s qty=%s", display_id, item_id, quantity)
        return result
