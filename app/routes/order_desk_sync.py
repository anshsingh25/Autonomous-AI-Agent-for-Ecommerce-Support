"""Secured HTTP hooks: sync order desk JSON ↔ Shopify (lines, totals, shipping)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from pydantic import BaseModel, Field

from app.database.factory import create_order_repository
from app.database.shopify_store import ShopifyRepository
from app.services.order_desk_store import get_order_desk_store
from app.utils.address import shipping_address_is_complete
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/order-desk", tags=["order-desk"])


def _verify_secret(
    x_order_desk_sync_secret: Annotated[
        str | None, Header(alias="X-Order-Desk-Sync-Secret")
    ] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    want = settings.order_desk_sync_secret.strip()
    if not want:
        raise HTTPException(
            status_code=503,
            detail="ORDER_DESK_SYNC_SECRET is not configured — sync routes disabled.",
        )
    got = (x_order_desk_sync_secret or "").strip()
    if not got or got != want:
        raise HTTPException(status_code=403, detail="Invalid sync secret")


def _normalize_oid(oid: str) -> str:
    return oid.strip().lstrip("#")


def _require_shopify(settings: Settings) -> ShopifyRepository:
    if settings.data_source.lower() != "shopify":
        raise HTTPException(
            status_code=400,
            detail="DATA_SOURCE must be shopify for Shopify synchronization.",
        )
    repo = create_order_repository(settings)
    if not isinstance(repo, ShopifyRepository):
        raise HTTPException(status_code=500, detail="Shopify repository is not configured.")
    return repo


class OrderDeskOrderBody(BaseModel):
    order_id: str = Field(..., min_length=1, description="Display order number, e.g. 1569")


class DeskSyncQtyBody(BaseModel):
    order_id: str = Field(..., min_length=1)
    quantity: int = Field(..., ge=1)
    line_index: int = Field(default=0, ge=0)
    product_name: str = Field(default="", description="Match line by title substring")


@router.post("/refresh-from-shopify")
async def refresh_desk_row_from_shopify(
    body: OrderDeskOrderBody,
    *,
    refresh_shipping: bool = Query(
        default=False,
        description=(
            "If true, overwrite desk shipping fields from Shopify shipping_address "
            "(only use when Shopify returns full address)."
        ),
    ),
    _auth: None = Depends(_verify_secret),
    settings: Settings = Depends(get_settings),
) -> dict:
    """
    Fetch the latest Shopify order row and merge line items + total (and optionally
    shipping) into ``order_desk.json``.
    """
    shopify = _require_shopify(settings)
    oid = _normalize_oid(body.order_id)
    desk = get_order_desk_store()
    if not desk.enabled():
        raise HTTPException(status_code=400, detail="Order desk JSON store is disabled or missing.")

    raw = await shopify.get_raw_order(oid)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Order {oid} not found in Shopify.")

    merged = desk.merge_snapshot_from_shopify_raw(
        oid,
        raw,
        refresh_shipping=refresh_shipping,
    )
    return {
        "success": True,
        "order_id": merged.get("order_id"),
        "total_inr": float(merged.get("total_inr") or 0),
        "financial_status": merged.get("financial_status"),
        "line_items_count": len(merged.get("line_items") or []),
    }


@router.post("/sync-quantity")
async def desk_sync_quantity_to_shopify_and_merge(
    body: DeskSyncQtyBody,
    _auth: None = Depends(_verify_secret),
    settings: Settings = Depends(get_settings),
) -> dict:
    """
    Updates quantity via Shopify Order Edit API, then rewrites desk line_items / total_inr
    from the refreshed Shopify order (pricing source of truth).
    """
    shopify = _require_shopify(settings)
    oid = _normalize_oid(body.order_id)
    desk = get_order_desk_store()
    if not desk.enabled():
        raise HTTPException(status_code=400, detail="Order desk disabled or missing.")

    result = await shopify.update_line_item_quantity(
        oid,
        quantity=body.quantity,
        product_name=body.product_name.strip(),
        line_index=body.line_index,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=str(result.get("message") or "Shopify qty failed"))

    raw = await shopify.get_raw_order(oid)
    if not raw:
        raise HTTPException(status_code=502, detail="Qty saved but Shopify order could not be reloaded.")

    desk.merge_snapshot_from_shopify_raw(oid, raw, refresh_shipping=False)
    return {"success": True, "shopify": result, "merged_total_inr": float(raw.get("total_price") or 0)}


@router.post("/push-save-to-shopify")
async def push_desk_shipping_to_shopify_then_merge(
    body: OrderDeskOrderBody,
    _auth: None = Depends(_verify_secret),
    settings: Settings = Depends(get_settings),
) -> dict:
    """
    After PHP (or scripts) saves an order desk row locally: push shipping to Shopify,
    append an order note for audit, and merge fresh line totals/prices back into the desk.
    """
    shopify = _require_shopify(settings)
    oid = _normalize_oid(body.order_id)
    desk = get_order_desk_store()
    if not desk.enabled():
        raise HTTPException(status_code=400, detail="Order desk disabled or missing.")

    rec = desk.get(oid)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Order #{oid} missing from order_desk.json")

    ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
    address1 = str(ship.get("address1") or "").strip()
    city = str(ship.get("city") or "").strip()
    zip_code = str(ship.get("zip_code") or ship.get("zip") or "").strip()
    if not address1 or not city or not zip_code:
        raise HTTPException(
            status_code=400,
            detail="Desk shipping missing address1, city, or zip — cannot sync to Shopify.",
        )

    name = str(
        ship.get("name") or rec.get("customer_name") or ""
    ).strip()
    province = str(ship.get("province") or "").strip()
    address2 = str(ship.get("address2") or "").strip()
    phone = str(rec.get("phone") or ship.get("phone") or "").strip()
    country = str(ship.get("country") or "India").strip()

    sy = await shopify.update_shipping_address(
        oid,
        address1=address1,
        city=city,
        zip_code=zip_code,
        province=province,
        address2=address2,
        name=name,
        phone=phone,
        country=country,
    )
    note_ok = {"success": False}
    if sy.get("success"):
        note_ok = await shopify.append_order_note(
            oid,
            (
                "Order desk: shipping/customer synced to Shopify from local UI "
                f"(delivery {city}, PIN {zip_code})."
            ),
        )

    raw = await shopify.get_raw_order(oid)
    merged = None
    if raw:
        merged = desk.merge_snapshot_from_shopify_raw(
            oid,
            raw,
            refresh_shipping=shipping_address_is_complete(raw.get("shipping_address")),
        )

    out: dict = {
        "success": True,
        "shopify_address": sy,
        "note": note_ok,
    }
    if merged:
        out["merged_total_inr"] = float(merged.get("total_inr") or 0)
    return out
