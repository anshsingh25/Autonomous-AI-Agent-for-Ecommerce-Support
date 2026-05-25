"""Order desk JSON store — same file as PHP order_desk (data/order_desk.json)."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.database.mock_db import Order
from app.utils.address import format_delivery_address, normalize_india_pin, shipping_address_is_complete
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _order_key(order_id: str) -> str:
    return str(order_id).lstrip("#").strip()


def full_delivery_address(rec: dict[str, Any]) -> str:
    """
    Full delivery line from order desk — saved delivery_address and/or shipping fields.
    Agent must read this verbatim on confirmation calls.
    """
    saved = str(rec.get("delivery_address") or "").strip()
    ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
    built = ""
    if ship:
        ship_fmt = dict(ship)
        if ship_fmt.get("zip_code") and not ship_fmt.get("zip"):
            ship_fmt["zip"] = ship_fmt["zip_code"]
        built = (format_delivery_address(ship_fmt) or "").strip()
    candidates = [c for c in (saved, built) if c]
    if not candidates:
        return ""
    return max(candidates, key=len)


def _parse_placed_at(raw: str | None) -> datetime:
    if not raw or not str(raw).strip():
        return datetime.now(timezone.utc)
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def desk_line_items_from_shopify_raw(raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Build ``line_items`` + spoken ``items`` list from Shopify REST order ``line_items``.
    """
    line_items: list[dict[str, Any]] = []
    items: list[str] = []
    for li in raw.get("line_items") or []:
        lid = li.get("id")
        if lid is None:
            continue
        title = str(li.get("title") or li.get("name") or "Item")
        qty = max(1, int(li.get("quantity") or 1))
        vt = li.get("variant_title")
        vt_s = str(vt).strip() if vt else ""
        label = title
        if vt_s and vt_s.lower() not in title.lower():
            label = f"{title} ({vt_s})"
        items.append(f"{label} x{qty}")
        try:
            price = float(li.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        line_items.append(
            {
                "line_item_id": int(lid),
                "title": title,
                "quantity": qty,
                "variant_title": vt_s or None,
                "sku": (str(li.get("sku") or "").strip() or None),
                "product_id": int(li["product_id"]) if li.get("product_id") else None,
                "variant_id": int(li["variant_id"]) if li.get("variant_id") else None,
                "price": price,
            }
        )
    return line_items, items


class OrderDeskStore:
    """Read/write orders imported via the PHP order desk UI."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        path = self._settings.order_desk_json_path.strip()
        self._path = Path(path) if path else Path("data/order_desk.json")

    def path(self) -> Path:
        return self._path

    def enabled(self) -> bool:
        if not self._settings.order_desk_enabled:
            return False
        return self._path.is_file()

    def load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"orders": {}, "meta": {}}
        raw = self._path.read_text(encoding="utf-8")
        if not raw.strip():
            return {"orders": {}, "meta": {}}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"orders": {}, "meta": {}}
        orders = data.get("orders")
        if not isinstance(orders, dict):
            orders = {}
        meta = data.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        return {"orders": orders, "meta": meta}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        self._path.write_text(payload, encoding="utf-8")

    def get(self, order_id: str) -> dict[str, Any] | None:
        key = _order_key(order_id)
        if not key:
            return None
        data = self.load()
        rec = data["orders"].get(key)
        return dict(rec) if isinstance(rec, dict) else None

    def list_orders(self, *, query: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        data = self.load()
        orders = [dict(v) for v in data["orders"].values() if isinstance(v, dict)]
        orders.sort(key=lambda o: int(o.get("order_id") or 0), reverse=True)
        if query and query.strip():
            q = query.strip().lower()
            orders = [
                o
                for o in orders
                if q in self._search_blob(o)
            ]
        return orders[:limit]

    @staticmethod
    def _search_blob(o: dict[str, Any]) -> str:
        ship = o.get("shipping") if isinstance(o.get("shipping"), dict) else {}
        return " ".join(
            [
                str(o.get("order_id", "")),
                str(o.get("customer_name", "")),
                str(o.get("phone", "")),
                str(o.get("email", "")),
                str(o.get("delivery_address", "")),
                " ".join(o.get("tags") or []),
                " ".join(o.get("items") or []),
                str(ship.get("phone", "")),
                str(ship.get("address1", "")),
                str(ship.get("city", "")),
            ]
        ).lower()

    def to_order(self, rec: dict[str, Any]) -> Order:
        ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
        phone = str(rec.get("phone") or ship.get("phone") or "").strip()
        delivery = full_delivery_address(rec)
        status = str(rec.get("status") or "processing")
        fin = str(rec.get("financial_status") or "").lower()
        if fin == "refunded":
            status = "refunded"
        return Order(
            order_id=str(rec.get("order_id", "")),
            customer_name=str(rec.get("customer_name") or "Customer"),
            phone=phone,
            status=status,
            total_inr=float(rec.get("total_inr") or 0),
            items=list(rec.get("items") or []),
            placed_at=_parse_placed_at(rec.get("placed_at")),
            delivery_address=delivery or None,
            cancel_allowed=status not in ("cancelled", "delivered", "refunded"),
        )

    def get_snapshot(self, order_id: str) -> dict[str, Any]:
        rec = self.get(order_id)
        if not rec:
            return {
                "found": False,
                "message": f"Order {order_id} not found in order desk. Import via order desk UI first.",
            }
        ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
        phone = str(rec.get("phone") or ship.get("phone") or "")
        line_items = rec.get("line_items") if isinstance(rec.get("line_items"), list) else []
        return {
            "found": True,
            "order_id": str(rec.get("order_id", order_id)),
            "customer_name": rec.get("customer_name"),
            "email": rec.get("email"),
            "items": list(rec.get("items") or []),
            "line_items": line_items,
            "total_inr": float(rec.get("total_inr") or 0),
            "status": rec.get("status") or "processing",
            "financial_status": rec.get("financial_status"),
            "fulfillment_status": rec.get("fulfillment_status"),
            "delivery_address": full_delivery_address(rec),
            "shipping_phone": phone,
            "shipping": ship,
            "tags": rec.get("tags") or [],
            "call_status": rec.get("call_status"),
            "notes": rec.get("notes"),
            "address_source": "order_desk",
            "editable": bool(rec.get("editable", True)),
            "data_source": "order_desk",
            "message": "Loaded from order desk (Shopify export / manual edits).",
        }

    def update_record(self, order_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge patch into order desk JSON (same fields as PHP save)."""
        key = _order_key(order_id)
        with _lock:
            data = self.load()
            if key not in data["orders"]:
                raise ValueError(f"Order #{key} not found in order desk.")
            rec = dict(data["orders"][key])
            new_key = _order_key(str(patch.get("order_id", key)))

            ship = dict(rec.get("shipping") or {}) if isinstance(rec.get("shipping"), dict) else {}
            for src, dst in (
                ("shipping_name", "name"),
                ("shipping_phone", "phone"),
                ("address1", "address1"),
                ("address2", "address2"),
                ("city", "city"),
                ("province", "province"),
                ("zip_code", "zip_code"),
                ("country", "country"),
            ):
                if src in patch and str(patch[src]).strip():
                    ship[dst] = str(patch[src]).strip()
            if ship:
                rec["shipping"] = ship

            scalar_map = {
                "customer_name": "customer_name",
                "phone": "phone",
                "email": "email",
                "delivery_address": "delivery_address",
                "total_inr": "total_inr",
                "financial_status": "financial_status",
                "fulfillment_status": "fulfillment_status",
                "placed_at": "placed_at",
                "call_status": "call_status",
                "notes": "notes",
                "status": "status",
            }
            for src, dst in scalar_map.items():
                if src in patch and patch[src] is not None:
                    rec[dst] = patch[src]

            if "items" in patch:
                raw_items = patch["items"]
                if isinstance(raw_items, list):
                    rec["items"] = raw_items
                else:
                    text = str(raw_items).strip()
                    rec["items"] = (
                        [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
                        if text
                        else []
                    )

            if "tags" in patch:
                raw_tags = patch["tags"]
                if isinstance(raw_tags, list):
                    rec["tags"] = raw_tags
                else:
                    rec["tags"] = [
                        t.strip() for t in str(raw_tags).split(",") if t.strip()
                    ]

            if "line_items" in patch and isinstance(patch["line_items"], list):
                rec["line_items"] = patch["line_items"]

            if "shopify_order_id" in patch and patch["shopify_order_id"] is not None:
                rec["shopify_order_id"] = int(patch["shopify_order_id"])

            if "synced_at" in patch and patch["synced_at"]:
                rec["synced_at"] = str(patch["synced_at"])

            if "dirty" in patch:
                rec["dirty"] = bool(patch["dirty"])

            if not rec.get("delivery_address") and ship:
                rec["delivery_address"] = format_delivery_address(ship)
            if not rec.get("phone") and ship.get("phone"):
                rec["phone"] = ship["phone"]

            rec["order_id"] = new_key
            rec["dirty"] = True
            rec["updated_at"] = datetime.now(timezone.utc).isoformat()
            rec["pii_limited"] = not rec.get("phone") and not (ship.get("address1") or "")

            del data["orders"][key]
            data["orders"][new_key] = rec
            data["meta"]["order_count"] = len(data["orders"])
            self._save(data)
            return rec

    def update_shipping_address(
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
    ) -> dict[str, Any]:
        rec = self.get(order_id)
        if not rec:
            return {"success": False, "message": "Order not found in order desk."}
        ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
        patch = {
            "address1": address1,
            "city": city,
            "zip_code": normalize_india_pin(zip_code),
            "province": province,
            "address2": address2,
            "shipping_name": name or rec.get("customer_name") or ship.get("name", ""),
            "shipping_phone": phone or rec.get("phone") or ship.get("phone", ""),
        }
        if phone:
            patch["phone"] = phone
        parts = [address1, address2, city, province, zip_code, ship.get("country", "India")]
        patch["delivery_address"] = ", ".join(p for p in parts if p and str(p).strip())
        updated = self.update_record(order_id, patch)
        return {
            "success": True,
            "delivery_address": updated.get("delivery_address"),
            "shipping_phone": updated.get("phone") or (updated.get("shipping") or {}).get("phone"),
            "message": "Shipping address saved to order desk.",
            "data_source": "order_desk",
        }

    def update_line_quantity(
        self,
        order_id: str,
        *,
        quantity: int,
        line_index: int = 0,
        product_name: str = "",
    ) -> dict[str, Any]:
        rec = self.get(order_id)
        if not rec:
            return {"success": False, "message": "Order not found in order desk."}
        items = list(rec.get("items") or [])
        line_items = list(rec.get("line_items") or [])
        qty = max(1, int(quantity))

        if line_items and line_index < len(line_items):
            li = dict(line_items[line_index])
            title = str(li.get("title") or "Item")
            li["quantity"] = qty
            line_items[line_index] = li
            spoken = f"{title} x{qty}"
            if line_index < len(items):
                items[line_index] = spoken
            else:
                items.append(spoken)
        elif items:
            idx = min(line_index, len(items) - 1)
            title = product_name or re.sub(r"\s*x\d+$", "", str(items[idx]), flags=re.I).strip()
            items[idx] = f"{title} x{qty}"
        else:
            title = product_name or "Item"
            items = [f"{title} x{qty}"]

        patch: dict[str, Any] = {"items": items, "line_items": line_items}
        line_total = 0.0
        for li in line_items:
            if not isinstance(li, dict):
                continue
            price = float(li.get("price") or 0)
            line_total += price * max(1, int(li.get("quantity") or 1))
        if line_total > 0:
            patch["total_inr"] = round(line_total, 2)

        updated = self.update_record(order_id, patch)
        return {
            "success": True,
            "items": updated.get("items"),
            "total_inr": float(updated.get("total_inr") or 0),
            "message": f"Quantity updated to {qty} in order desk JSON.",
            "data_source": "order_desk",
            "shopify_sync": "not_attempted",
        }

    def merge_snapshot_from_shopify_raw(
        self,
        order_id: str,
        raw: dict[str, Any],
        *,
        refresh_shipping: bool = False,
    ) -> dict[str, Any]:
        """
        Overwrite pricing lines / totals from a fresh Shopify REST order payload.

        When ``refresh_shipping`` is True, merges shipping_address into the desk
        block (caller should only set this after a successful Shopify shipping update).
        """
        oid = _order_key(order_id)
        line_items, items = desk_line_items_from_shopify_raw(raw)
        patch: dict[str, Any] = {
            "line_items": line_items,
            "items": items,
            "total_inr": float(raw.get("total_price") or 0),
            "financial_status": str(raw.get("financial_status") or ""),
            "shopify_order_id": int(raw["id"]),
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "dirty": False,
        }
        if refresh_shipping:
            sh = dict(raw.get("shipping_address") or {})
            if shipping_address_is_complete(sh):
                name = str(sh.get("name") or "").strip()
                if not name:
                    fn = str(sh.get("first_name") or "").strip()
                    ln = str(sh.get("last_name") or "").strip()
                    name = (fn + " " + ln).strip()
                phone = str(sh.get("phone") or "").strip()
                patch.update(
                    {
                        "shipping_name": name,
                        "address1": str(sh.get("address1") or ""),
                        "address2": str(sh.get("address2") or ""),
                        "city": str(sh.get("city") or ""),
                        "province": str(sh.get("province") or ""),
                        "zip_code": str(sh.get("zip") or sh.get("zip_code") or ""),
                        "country": str(sh.get("country") or "India"),
                        "shipping_phone": phone,
                    }
                )
                if phone:
                    patch["phone"] = phone
                fmt = format_delivery_address(sh)
                if fmt:
                    patch["delivery_address"] = fmt

        updated = self.update_record(oid, patch)
        return updated

    def append_tags(self, order_id: str, tags: list[str]) -> None:
        rec = self.get(order_id)
        if not rec:
            return
        existing = list(rec.get("tags") or [])
        merged = existing + [t for t in tags if t and t not in existing]
        self.update_record(order_id, {"tags": merged})


_store: OrderDeskStore | None = None


def get_order_desk_store() -> OrderDeskStore:
    global _store
    if _store is None:
        _store = OrderDeskStore()
    return _store


def order_desk_primary() -> bool:
    """Agent should use order desk as the source of truth for order PII."""
    return get_order_desk_store().enabled()
