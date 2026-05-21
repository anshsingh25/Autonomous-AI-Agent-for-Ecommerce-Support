"""Apply outbound confirmation edits and refresh call context."""

from __future__ import annotations

import logging
from typing import Any

from app.database.factory import create_order_repository
from app.database.shopify_store import ShopifyRepository
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import get_context, save_context
from app.utils.address import shipping_address_is_complete
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def _shopify_repo() -> ShopifyRepository | None:
    settings = get_settings()
    if settings.data_source.lower() != "shopify":
        return None
    repo = create_order_repository(settings)
    return repo if isinstance(repo, ShopifyRepository) else None


async def refresh_outbound_context(ctx: OutboundCallContext) -> OutboundCallContext:
    """Reload order totals, address, and line items from Shopify after an edit."""
    shopify = _shopify_repo()
    if not shopify:
        return ctx

    snap = await shopify.get_confirmation_order_snapshot(ctx.order_id)
    if not snap.get("found"):
        return ctx

    raw = await shopify.get_raw_order(ctx.order_id)
    if raw:
        ctx.shopify_order_id = int(raw["id"])
        ctx.line_items = shopify.line_items_from_raw(raw)

    ctx.items = list(snap.get("items") or ctx.items)
    ctx.total_inr = float(snap.get("total_inr") or ctx.total_inr)
    ctx.status = str(snap.get("status") or ctx.status)
    new_addr = snap.get("delivery_address")
    if new_addr and (
        not ctx.delivery_address
        or shipping_address_is_complete(
            (raw or {}).get("shipping_address") if raw else None
        )
        or len(str(new_addr)) > len(str(ctx.delivery_address or ""))
    ):
        ctx.delivery_address = new_addr
    save_context(ctx)
    return ctx


async def apply_address_update(
    ctx: OutboundCallContext,
    *,
    address1: str,
    city: str,
    zip_code: str,
    province: str = "",
    address2: str = "",
    name: str = "",
    phone: str = "",
) -> dict[str, Any]:
    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Address updates require DATA_SOURCE=shopify",
        }

    result = await shopify.update_shipping_address(
        ctx.order_id,
        address1=address1,
        city=city,
        zip_code=zip_code,
        province=province,
        address2=address2,
        name=name or ctx.customer_name,
        phone=phone or ctx.customer_phone,
    )
    if result.get("success"):
        ctx.delivery_address = result.get("delivery_address") or ctx.delivery_address
        await refresh_outbound_context(ctx)
    return result


async def apply_quantity_update(
    ctx: OutboundCallContext,
    *,
    quantity: int,
    product_name: str = "",
    line_index: int = 0,
) -> dict[str, Any]:
    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Quantity updates require DATA_SOURCE=shopify",
        }

    result = await shopify.update_line_item_quantity(
        ctx.order_id,
        quantity=quantity,
        product_name=product_name,
        line_index=line_index,
    )
    if result.get("success"):
        ctx.total_inr = float(result.get("total_inr") or ctx.total_inr)
        await refresh_outbound_context(ctx)
    return result


async def apply_variant_swap(
    ctx: OutboundCallContext,
    *,
    new_variant: str,
    product_name: str = "",
    line_index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Variant changes require DATA_SOURCE=shopify",
        }

    result = await shopify.swap_line_item_variant(
        ctx.order_id,
        new_variant=new_variant,
        product_name=product_name,
        line_index=line_index,
        quantity=quantity,
    )
    if result.get("success"):
        ctx.total_inr = float(result.get("total_inr") or ctx.total_inr)
        await refresh_outbound_context(ctx)
    return result


async def list_variants_for_item(
    ctx: OutboundCallContext,
    *,
    product_name: str = "",
    line_index: int = 0,
) -> dict[str, Any]:
    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Variant listing requires DATA_SOURCE=shopify",
        }
    return await shopify.list_item_variants(
        ctx.order_id,
        product_name=product_name,
        line_index=line_index,
    )
