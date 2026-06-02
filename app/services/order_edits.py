"""Order edits for inbound and outbound (Magento / Shopify / optional order desk)."""

from __future__ import annotations

import logging
from typing import Any

from app.database.factory import create_order_repository
from app.database.magento_store import MagentoRepository
from app.database.shopify_store import ShopifyRepository
from app.services.order_desk_store import get_order_desk_store, order_desk_primary
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def _default_ship_country() -> str:
    if get_settings().data_source.lower().strip() == "magento":
        return "GB"
    return "India"


def _shopify_sync_failed_message(result: dict[str, Any]) -> str:
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


def apply_shopify_truth_to_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Do not report success if only order_desk.json changed while Shopify is configured."""
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


def _magento_repo() -> MagentoRepository | None:
    settings = get_settings()
    if settings.data_source.lower() != "magento":
        return None
    repo = create_order_repository(settings)
    return repo if isinstance(repo, MagentoRepository) else None


async def _audit_note(order_id: str, line: str) -> None:
    platform = _shopify_repo() or _magento_repo()
    if platform:
        try:
            await platform.append_order_note(order_id, line)
        except Exception:
            logger.exception("Could not append order note for %s", order_id)


async def apply_address_update_for_order(
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
    country = _default_ship_country()
    if order_desk_primary():
        desk = get_order_desk_store()
        result = desk.update_shipping_address(
            order_id,
            address1=address1,
            city=city,
            zip_code=zip_code,
            province=province,
            address2=address2,
            name=name,
            phone=phone,
        )
        if result.get("success"):
            shopify = _shopify_repo()
            if shopify:
                try:
                    sr = await shopify.update_shipping_address(
                        order_id,
                        address1=address1,
                        city=city,
                        zip_code=zip_code,
                        province=province,
                        address2=address2,
                        name=name,
                        phone=phone,
                        country=country,
                    )
                    if sr.get("success"):
                        await shopify.append_order_note(
                            order_id,
                            "Delivery address synced from order desk / voice support.",
                        )
                        raw = await shopify.get_raw_order(order_id)
                        if raw:
                            desk.merge_snapshot_from_shopify_raw(
                                order_id, raw, refresh_shipping=True
                            )
                            result["shopify_sync"] = "ok"
                    else:
                        result["shopify_sync"] = "failed"
                        result["shopify_message"] = sr.get("message", "")
                except Exception:
                    logger.exception("Shopify address sync failed (desk saved locally)")
                    result["shopify_sync"] = "error"
            else:
                result["shopify_sync"] = "not_attempted"
            return apply_shopify_truth_to_tool_result(result)
        return result

    magento = _magento_repo()
    if magento:
        result = await magento.update_shipping_address(
            order_id,
            address1=address1,
            city=city,
            zip_code=zip_code,
            province=province,
            address2=address2,
            name=name,
            phone=phone,
            country=country,
        )
        if result.get("success"):
            await _audit_note(order_id, "Inbound/outbound voice: shipping address updated.")
        return result

    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Address updates require DATA_SOURCE=shopify/magento (or order desk)",
        }

    result = await shopify.update_shipping_address(
        order_id,
        address1=address1,
        city=city,
        zip_code=zip_code,
        province=province,
        address2=address2,
        name=name,
        phone=phone,
        country=country,
    )
    if result.get("success"):
        await _audit_note(order_id, "Voice support: shipping address updated.")
        result["shopify_sync"] = "ok"
        result["shopify_updated"] = True
    return result


async def apply_quantity_update_for_order(
    order_id: str,
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
                order_id,
                quantity=quantity,
                product_name=product_name,
                line_index=line_index,
            )
            if sy.get("success"):
                raw = await shopify.get_raw_order(order_id)
                if raw:
                    get_order_desk_store().merge_snapshot_from_shopify_raw(
                        order_id, raw, refresh_shipping=False
                    )
                sy["sync_source"] = "shopify_then_desk"
                sy["shopify_sync"] = "ok"
                sy["shopify_updated"] = True
                return sy
            shopify_desk_fallback = True
        except Exception:
            shopify_desk_fallback = True
            logger.exception("Shopify qty sync crashed for order %s", order_id)

    if order_desk_primary():
        result = get_order_desk_store().update_line_quantity(
            order_id,
            quantity=quantity,
            product_name=product_name,
            line_index=line_index,
        )
        if shopify_desk_fallback and result.get("success"):
            result["shopify_sync"] = "failed"
            result["warning"] = (
                "Shopify Order Edit API failed — only order_desk.json was updated."
            )
            return apply_shopify_truth_to_tool_result(result)
        return result

    magento = _magento_repo()
    if magento:
        result = await magento.update_line_item_quantity(
            order_id,
            quantity=quantity,
            product_name=product_name,
            line_index=line_index,
        )
        if result.get("success"):
            await _audit_note(order_id, f"Voice support: line quantity set to {quantity}.")
        return result

    if not shopify:
        return {
            "success": False,
            "message": "Quantity updates require DATA_SOURCE=shopify/magento (or order desk)",
        }

    result = await shopify.update_line_item_quantity(
        order_id,
        quantity=quantity,
        product_name=product_name,
        line_index=line_index,
    )
    if result.get("success"):
        await _audit_note(order_id, f"Voice support: line quantity set to {quantity}.")
    return result


async def apply_variant_swap_for_order(
    order_id: str,
    *,
    new_variant: str,
    product_name: str = "",
    line_index: int = 0,
    quantity: int | None = None,
) -> dict[str, Any]:
    if order_desk_primary():
        desk = get_order_desk_store()
        rec = desk.get(order_id)
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
            order_id,
            {"items": items, "notes": (rec.get("notes") or "") + f" Variant: {new_variant}"},
        )
        snap = desk.get_snapshot(order_id)
        return {
            "success": True,
            "message": f"Variant note saved in order desk: {new_variant}",
            "data_source": "order_desk",
            "items": snap.get("items"),
        }

    if _magento_repo():
        return {
            "success": False,
            "message": (
                "Product variant swaps are not supported on Magento via voice. "
                "Add a status comment in Admin or ask the customer to accept delivery and return."
            ),
        }

    shopify = _shopify_repo()
    if not shopify:
        return {
            "success": False,
            "message": "Variant changes require DATA_SOURCE=shopify",
        }
    return await shopify.swap_line_item_variant(
        order_id,
        new_variant=new_variant,
        product_name=product_name,
        line_index=line_index,
        quantity=quantity,
    )


async def list_variants_for_order(
    order_id: str,
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
                "Ask the customer which option they want."
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
        order_id,
        product_name=product_name,
        line_index=line_index,
    )


async def get_order_edit_snapshot(order_id: str) -> dict[str, Any]:
    """Fresh order facts after an edit (live API unless order desk is primary)."""
    if not order_desk_primary():
        magento = _magento_repo()
        if magento:
            return await magento.get_confirmation_order_snapshot(order_id)
        shopify = _shopify_repo()
        if shopify:
            return await shopify.get_confirmation_order_snapshot(order_id)

    desk = get_order_desk_store()
    if order_desk_primary():
        return desk.get_snapshot(order_id)
    magento = _magento_repo()
    if magento:
        return await magento.get_confirmation_order_snapshot(order_id)
    shopify = _shopify_repo()
    if shopify:
        return await shopify.get_confirmation_order_snapshot(order_id)
    return {"found": False, "message": "No order data source configured."}


async def enrich_edit_result(order_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Attach current delivery_address, items, and total after a successful edit."""
    if not result.get("success"):
        return result
    snap = await get_order_edit_snapshot(order_id)
    if not snap.get("found"):
        return result
    if snap.get("delivery_address"):
        result["delivery_address"] = snap["delivery_address"]
    if snap.get("items"):
        result["items"] = snap["items"]
    if snap.get("total_inr") is not None:
        result["total_inr"] = snap["total_inr"]
    if snap.get("shipping_phone"):
        result["shipping_phone"] = snap["shipping_phone"]
    result["order_id"] = snap.get("order_id", result.get("order_id", order_id))
    return result
