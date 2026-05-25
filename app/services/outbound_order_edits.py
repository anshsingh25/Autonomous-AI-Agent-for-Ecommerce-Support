"""Apply outbound confirmation edits and refresh call context."""

from __future__ import annotations

import logging
from typing import Any

from app.database.factory import create_order_repository
from app.database.shopify_store import ShopifyRepository
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import get_context, save_context
from app.services.order_desk_store import get_order_desk_store, order_desk_primary
from app.utils.address import shipping_address_is_complete
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def _shopify_sync_failed_message(result: dict[str, Any]) -> str:
    """Human-readable reason Shopify Admin may still show old data."""
    if result.get("shopify_sync") == "ok":
        return ""
    msg = str(result.get("shopify_message") or result.get("warning") or "").strip()
    if result.get("shopify_sync") == "failed":
        return msg or "Shopify API rejected the change."
    if result.get("shopify_sync") == "error":
        return msg or "Shopify sync error (check server logs)."
    if result.get("shopify_sync") == "not_attempted":
        return (
            "Change saved only in order desk JSON — Shopify was not updated. "
            "Refresh order desk UI or configure ORDER_DESK_SYNC_SECRET for PHP→Shopify push."
        )
    return msg


def _apply_shopify_truth_to_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    When Shopify is configured, do not report bare success if only order_desk.json changed.
    Prevents the agent saying 'updated' while Shopify Admin still shows old data.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return result
    if not _shopify_repo():
        return result
    if result.get("shopify_sync") == "ok" or result.get("sync_source") == "shopify_then_desk":
        result["shopify_updated"] = True
        return result
    desk_saved = result.get("data_source") == "order_desk" or result.get("order_desk_saved")
    if not desk_saved:
        return result
    why = _shopify_sync_failed_message(result)
    result["shopify_updated"] = False
    result["order_desk_saved"] = True
    result["success"] = False
    result["message"] = (
        f"Saved locally in order desk only. Shopify Admin was NOT updated. {why}"
    )
    return result


def _shopify_repo() -> ShopifyRepository | None:
    settings = get_settings()
    if settings.data_source.lower() != "shopify":
        return None
    repo = create_order_repository(settings)
    return repo if isinstance(repo, ShopifyRepository) else None


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
    """Reload order facts from order desk (preferred) or Shopify."""
    desk = get_order_desk_store()
    if order_desk_primary():
        snap = desk.get_snapshot(ctx.order_id)
        if snap.get("found"):
            _apply_desk_snapshot_to_ctx(ctx, snap)
            save_context(ctx)
            return ctx

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
    if order_desk_primary():
        desk = get_order_desk_store()
        result = desk.update_shipping_address(
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
            shopify = _shopify_repo()
            if shopify:
                try:
                    sr = await shopify.update_shipping_address(
                        ctx.order_id,
                        address1=address1,
                        city=city,
                        zip_code=zip_code,
                        province=province,
                        address2=address2,
                        name=name or ctx.customer_name,
                        phone=phone or ctx.customer_phone,
                    )
                    if sr.get("success"):
                        await shopify.append_order_note(
                            ctx.order_id,
                            "Delivery shipping address synced from order desk / outbound confirmation.",
                        )
                        raw = await shopify.get_raw_order(ctx.order_id)
                        if raw:
                            desk.merge_snapshot_from_shopify_raw(
                                ctx.order_id, raw, refresh_shipping=True
                            )
                            result["shopify_sync"] = "ok"
                    else:
                        result["shopify_sync"] = "failed"
                        result["shopify_message"] = sr.get("message", "")
                        logger.warning(
                            "Desk address saved locally but Shopify refused: %s",
                            sr.get("message"),
                        )
                except Exception:
                    logger.exception("Shopify address sync failed (desk saved locally)")
                    result["shopify_sync"] = "error"
            else:
                result["shopify_sync"] = "not_attempted"

            await refresh_outbound_context(ctx)
            return _apply_shopify_truth_to_tool_result(result)
        return result

    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Address updates require order desk or DATA_SOURCE=shopify",
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
        result["shopify_sync"] = "ok"
        result["shopify_updated"] = True
    return result


async def apply_quantity_update(
    ctx: OutboundCallContext,
    *,
    quantity: int,
    product_name: str = "",
    line_index: int = 0,
) -> dict[str, Any]:
    shopify_desk_fallback = False
    shopify = _shopify_repo()
    if order_desk_primary() and shopify:
        try:
            sy = await shopify.update_line_item_quantity(
                ctx.order_id,
                quantity=quantity,
                product_name=product_name,
                line_index=line_index,
            )
            if sy.get("success"):
                raw = await shopify.get_raw_order(ctx.order_id)
                if raw:
                    desk = get_order_desk_store()
                    desk.merge_snapshot_from_shopify_raw(
                        ctx.order_id, raw, refresh_shipping=False
                    )
                ctx.total_inr = float(sy.get("total_inr") or ctx.total_inr)
                await refresh_outbound_context(ctx)
                sy["sync_source"] = "shopify_then_desk"
                sy["shopify_sync"] = "ok"
                sy["shopify_updated"] = True
                return sy
            shopify_desk_fallback = True
            logger.warning(
                "Shopify quantity update unavailable for order %s: %s — falling back to desk JSON.",
                ctx.order_id,
                sy.get("message"),
            )
        except Exception:
            shopify_desk_fallback = True
            logger.exception(
                "Shopify qty sync crashed for order %s — falling back to desk JSON.",
                ctx.order_id,
            )

    if order_desk_primary():
        desk = get_order_desk_store()
        result = desk.update_line_quantity(
            ctx.order_id,
            quantity=quantity,
            product_name=product_name,
            line_index=line_index,
        )
        if result.get("success"):
            ctx.total_inr = float(result.get("total_inr") or ctx.total_inr)
            await refresh_outbound_context(ctx)
        if shopify_desk_fallback and result.get("success"):
            result["shopify_sync"] = "failed"
            result["warning"] = (
                "Shopify Order Edit API failed — only order_desk.json was updated. "
                "Shopify Admin quantity/total will not match until sync succeeds."
            )
            return _apply_shopify_truth_to_tool_result(result)
        return result

    shopify_only = shopify
    if not shopify_only:
        return {
            "success": False,
            "message": "Quantity updates require order desk or DATA_SOURCE=shopify",
        }

    result = await shopify_only.update_line_item_quantity(
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
    if order_desk_primary():
        desk = get_order_desk_store()
        rec = desk.get(ctx.order_id)
        if not rec:
            return {"success": False, "message": "Order not found in order desk."}
        items = list(rec.get("items") or [])
        label = product_name or (items[line_index] if line_index < len(items) else "Item")
        new_line = f"{label} ({new_variant})"
        if quantity is not None:
            new_line += f" x{quantity}"
        if line_index < len(items):
            items[line_index] = new_line
        else:
            items.append(new_line)
        desk.update_record(
            ctx.order_id,
            {"items": items, "notes": (rec.get("notes") or "") + f" Variant: {new_variant}"},
        )
        await refresh_outbound_context(ctx)
        snap = desk.get_snapshot(ctx.order_id)
        return {
            "success": True,
            "message": f"Variant note saved in order desk: {new_variant}",
            "data_source": "order_desk",
            "items": snap.get("items"),
        }

    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Variant changes require order desk or DATA_SOURCE=shopify",
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
    if order_desk_primary():
        return {
            "success": True,
            "variants": [],
            "message": (
                "Variant list is not available from order desk export. "
                "Ask the customer which option they want and note it in the call."
            ),
            "data_source": "order_desk",
        }

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


async def get_confirmation_snapshot(order_id: str) -> dict[str, Any]:
    """Snapshot for tools — order desk first."""
    desk = get_order_desk_store()
    if order_desk_primary():
        return desk.get_snapshot(order_id)
    shopify = _shopify_repo()
    if shopify:
        return await shopify.get_confirmation_order_snapshot(order_id)
    return {"found": False, "message": "No order data source configured."}
