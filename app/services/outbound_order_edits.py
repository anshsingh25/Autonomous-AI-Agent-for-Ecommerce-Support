"""Outbound confirmation edits — thin wrappers over shared order_edits."""

from __future__ import annotations

from typing import Any

from app.database.shopify_store import ShopifyRepository
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import save_context
from app.services.order_desk_store import get_order_desk_store, order_desk_primary
from app.services.order_edits import (
    apply_address_update_for_order,
    apply_quantity_update_for_order,
    apply_shopify_truth_to_tool_result,
    apply_variant_swap_for_order,
    get_order_edit_snapshot,
    list_variants_for_order,
)
from app.utils.address import shipping_address_is_complete
from app.utils.config import get_settings
from app.database.factory import create_order_repository
from app.database.magento_store import MagentoRepository


def _shopify_repo() -> ShopifyRepository | None:
    settings = get_settings()
    if settings.data_source.lower() != "shopify":
        return None
    repo = create_order_repository(settings)
    return repo if isinstance(repo, ShopifyRepository) else None


def _magento_repo() -> MagentoRepository | None:
    settings = get_settings()
    if settings.data_source.lower() != "magento":
        return None
    repo = create_order_repository(settings)
    return repo if isinstance(repo, MagentoRepository) else None


def _apply_desk_snapshot_to_ctx(ctx: OutboundCallContext, snap: dict[str, Any]) -> None:
    if not snap.get("found"):
        return
    ctx.customer_name = str(snap.get("customer_name") or ctx.customer_name)
    ctx.items = list(snap.get("items") or ctx.items)
    ctx.total_inr = float(snap.get("total_inr") or ctx.total_inr)
    ctx.status = str(snap.get("status") or ctx.status)
    phone = str(snap.get("shipping_phone") or "").strip()
    if phone:
        ctx.customer_phone = phone
    addr = snap.get("delivery_address")
    if addr:
        ctx.delivery_address = str(addr)


async def refresh_outbound_context(ctx: OutboundCallContext) -> OutboundCallContext:
    """Reload order facts from order desk (if enabled) or the live store API."""
    desk = get_order_desk_store()
    if order_desk_primary():
        snap = desk.get_snapshot(ctx.order_id)
        if snap.get("found"):
            _apply_desk_snapshot_to_ctx(ctx, snap)
            save_context(ctx)
            return ctx

    platform = _shopify_repo() or _magento_repo()
    if not platform:
        return ctx

    snap = await platform.get_confirmation_order_snapshot(ctx.order_id)
    if not snap.get("found"):
        return ctx

    raw = await platform.get_raw_order(ctx.order_id)
    if raw:
        entity = raw.get("id") or raw.get("entity_id")
        if entity:
            ctx.shopify_order_id = int(entity)
        ctx.line_items = platform.line_items_from_raw(raw)

    ctx.items = list(snap.get("items") or ctx.items)
    ctx.total_inr = float(snap.get("total_inr") or ctx.total_inr)
    ctx.status = str(snap.get("status") or ctx.status)
    new_addr = snap.get("delivery_address")
    if new_addr and (
        not ctx.delivery_address
        or (
            isinstance(platform, ShopifyRepository)
            and shipping_address_is_complete(
                (raw or {}).get("shipping_address") if raw else None
            )
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
    result = await apply_address_update_for_order(
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
        if result.get("shipping_phone"):
            ctx.customer_phone = str(result["shipping_phone"])
        await refresh_outbound_context(ctx)
    return result


async def apply_quantity_update(
    ctx: OutboundCallContext,
    *,
    quantity: int,
    product_name: str = "",
    line_index: int = 0,
) -> dict[str, Any]:
    result = await apply_quantity_update_for_order(
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
    result = await apply_variant_swap_for_order(
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
    return await list_variants_for_order(
        ctx.order_id,
        product_name=product_name,
        line_index=line_index,
    )


async def get_confirmation_snapshot(order_id: str) -> dict[str, Any]:
    return await get_order_edit_snapshot(order_id)


# Backward-compatible alias for Shopify desk truth helper
_apply_shopify_truth_to_tool_result = apply_shopify_truth_to_tool_result
