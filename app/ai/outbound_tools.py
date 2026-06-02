"""Outbound order-confirmation tools only (not used on inbound support)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams, LLMService

from app.ai.tool_hold import speak_tool_hold
from app.ai.tool_parsing import parse_quantity
from app.database.factory import create_order_repository
from app.database.magento_store import MagentoRepository
from app.database.shopify_store import ShopifyRepository
from app.services.call_context_store import get_context, save_context
from app.ai.outbound_call_state import remind_after_tool
from app.services.order_desk_store import order_desk_primary
from app.services.outbound_order_edits import (
    apply_address_update,
    apply_quantity_update,
    apply_variant_swap,
    get_confirmation_snapshot,
    list_variants_for_item,
    refresh_outbound_context,
)
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def build_outbound_tools_schema() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="refresh_confirmation_order",
                description=(
                    "Reload current order details from the order desk for this confirmation call. "
                    "Use after any change or if you need updated total/address/phone."
                ),
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="update_delivery_address",
                description=(
                    "Update shipping address in the order desk (saved for fulfillment). "
                    "Customer should give full address in one natural sentence when possible. "
                    "Read back once (house/area, city, PIN) and get yes, then call. "
                    "Only ask for missing city or PIN if not provided — do not interrogate field-by-field. "
                    "Say a hold line before calling."
                ),
                properties={
                    "address1": {
                        "type": "string",
                        "description": "House / street / area line",
                    },
                    "city": {"type": "string", "description": "City"},
                    "zip_code": {"type": "string", "description": "PIN / zip code"},
                    "province": {
                        "type": "string",
                        "description": "State (optional)",
                    },
                    "address2": {
                        "type": "string",
                        "description": "Landmark / second line (optional)",
                    },
                    "phone": {
                        "type": "string",
                        "description": (
                            "Mobile on shipping address (10-digit India or +91). "
                            "Omit to keep existing shipping phone."
                        ),
                    },
                },
                required=["address1", "city", "zip_code"],
            ),
            FunctionSchema(
                name="update_item_quantity",
                description=(
                    "Change quantity on a line item (unfulfilled orders only). "
                    "Confirm new qty with customer first. Say a hold line before calling."
                ),
                properties={
                    "quantity": {
                        "type": "integer",
                        "description": "New quantity (minimum 1)",
                    },
                    "product_name": {
                        "type": "string",
                        "description": "Product title to match (optional if single item)",
                    },
                    "line_index": {
                        "type": "integer",
                        "description": "0-based line index if multiple products (default 0)",
                    },
                },
                required=["quantity"],
            ),
            FunctionSchema(
                name="list_item_variants",
                description=(
                    "List available sizes/colours (variants) in stock for an order line. "
                    "Use before swap when customer asks what is available."
                ),
                properties={
                    "product_name": {
                        "type": "string",
                        "description": "Product title to match (optional if single item)",
                    },
                    "line_index": {
                        "type": "integer",
                        "description": "0-based line index (default 0)",
                    },
                },
                required=[],
            ),
            FunctionSchema(
                name="swap_item_variant",
                description=(
                    "Change size, colour, or variant on this order (unfulfilled only). "
                    "Pass what customer said e.g. 'Medium', 'Black', 'Size L'. "
                    "Confirm with customer first; use list_item_variants if unsure. "
                    "Say a hold line before calling."
                ),
                properties={
                    "new_variant": {
                        "type": "string",
                        "description": "Requested size/colour/variant label",
                    },
                    "product_name": {
                        "type": "string",
                        "description": "Product title if multiple items on order",
                    },
                    "line_index": {
                        "type": "integer",
                        "description": "0-based line index (default 0)",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Qty after swap (default: keep same as before)",
                    },
                },
                required=["new_variant"],
            ),
            FunctionSchema(
                name="cancel_confirmation_order",
                description=(
                    "Cancel this order when customer clearly wants to cancel (not 'not now'). "
                    "Say you are processing cancellation before calling."
                ),
                properties={
                    "reason": {
                        "type": "string",
                        "description": "Short cancellation reason from customer",
                    },
                },
                required=[],
            ),
        ]
    )


def register_outbound_confirmation_tools(
    llm: LLMService,
    session_id: str,
) -> None:
    """Register tools bound to one outbound call session (order already known)."""
    settings = get_settings()
    shopify: ShopifyRepository | None = None
    magento: MagentoRepository | None = None
    ds = settings.data_source.lower()
    if ds == "shopify":
        repo = create_order_repository(settings)
        if isinstance(repo, ShopifyRepository):
            shopify = repo
    elif ds == "magento":
        repo = create_order_repository(settings)
        if isinstance(repo, MagentoRepository):
            magento = repo
    platform = shopify or magento

    def _ctx_or_error() -> tuple[Any, dict | None]:
        ctx = get_context(session_id)
        if not ctx:
            return None, {"success": False, "message": "Call session expired"}
        return ctx, None

    async def _attach_verified_order_snapshot(ctx, result: dict) -> dict:
        """
        Add a fresh Shopify snapshot after successful edits.

        This gives the LLM deterministic post-update facts (address/qty/total)
        so it can confirm accurately on the call.
        """
        if not isinstance(result, dict) or not result.get("success"):
            return result
        try:
            await refresh_outbound_context(ctx)
            snap = await get_confirmation_snapshot(ctx.order_id)
            if snap.get("found"):
                result["verified_order"] = {
                    "order_id": snap.get("order_id"),
                    "items": snap.get("items"),
                    "total_inr": snap.get("total_inr"),
                    "delivery_address": ctx.delivery_address or snap.get("delivery_address"),
                    "shipping_phone": ctx.customer_phone,
                    "status": snap.get("status"),
                    "data_source": snap.get("data_source", "order_desk"),
                }
        except Exception:
            logger.exception("Failed to attach verified snapshot for order=%s", ctx.order_id)
        return result

    async def _safe(params: FunctionCallParams, fn, tool_name: str) -> None:
        try:
            logger.info("Outbound tool call %s args=%s", tool_name, params.arguments)
            await speak_tool_hold(params)
            await fn(params)
        except Exception:
            logger.exception("Outbound tool %s failed", tool_name)
            await params.result_callback(
                json.dumps(
                    {
                        "success": False,
                        "message": "System error — try again in a moment",
                    }
                )
            )

    async def refresh_confirmation_order(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            await refresh_outbound_context(ctx)
            snap = await get_confirmation_snapshot(ctx.order_id)
            if not snap.get("found") and not platform:
                await p.result_callback(
                    json.dumps(
                        {
                            "found": True,
                            "order_id": ctx.order_id,
                            "items": ctx.items,
                            "total_inr": ctx.total_inr,
                            "delivery_address": ctx.delivery_address,
                            "shipping_phone": ctx.customer_phone,
                            "data_source": "call_context",
                            "message": "Using cached call context (order desk empty)",
                        }
                    )
                )
                return
            await p.result_callback(json.dumps(snap))

        await _safe(params, _run, "refresh_confirmation_order")

    async def update_delivery_address(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await apply_address_update(
                ctx,
                address1=str(p.arguments.get("address1", "")),
                city=str(p.arguments.get("city", "")),
                zip_code=str(p.arguments.get("zip_code", "")),
                province=str(p.arguments.get("province", "")),
                address2=str(p.arguments.get("address2", "")),
                phone=str(p.arguments.get("phone", "")),
            )
            result = await _attach_verified_order_snapshot(ctx, result)
            await remind_after_tool(p.llm, ctx, "update_delivery_address", result)
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "update_delivery_address")

    async def update_item_quantity(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            qty = parse_quantity(p.arguments.get("quantity"))
            if qty is None:
                await p.result_callback(
                    json.dumps(
                        {
                            "success": False,
                            "message": (
                                "I could not clearly understand the new quantity. "
                                "Please say it as digit and word, for example: 3 (three)."
                            ),
                        }
                    )
                )
                return
            result = await apply_quantity_update(
                ctx,
                quantity=qty,
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
            )
            result = await _attach_verified_order_snapshot(ctx, result)
            await remind_after_tool(p.llm, ctx, "update_item_quantity", result)
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "update_item_quantity")

    async def list_item_variants(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await list_variants_for_item(
                ctx,
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
            )
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "list_item_variants")

    async def swap_item_variant(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            qty_raw = p.arguments.get("quantity")
            quantity = int(qty_raw) if qty_raw is not None else None
            result = await apply_variant_swap(
                ctx,
                new_variant=str(p.arguments.get("new_variant", "")),
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
                quantity=quantity,
            )
            result = await _attach_verified_order_snapshot(ctx, result)
            await remind_after_tool(p.llm, ctx, "swap_item_variant", result)
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "swap_item_variant")

    async def cancel_confirmation_order(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            from app.services.order_service import order_service

            reason = str(p.arguments.get("reason", "")).strip()
            if order_desk_primary():
                result = await order_service.cancel_order(ctx.order_id, reason=reason)
            elif platform:
                result = await platform.cancel_order_api(
                    ctx.order_id,
                    reason=reason or "Customer cancelled during AI confirmation call",
                )
            else:
                result = {
                    "success": False,
                    "message": "Cancellation requires order desk or ecommerce API",
                }
            if result.get("success"):
                from app.models.call_outcome import CallOutcome

                ctx.shopify_cancel_via_tool = True
                ctx.outcome = CallOutcome.CANCELLED
                ctx.final_confirm_received = True
                ctx.agent_turns_after_final_confirm = 0
                save_context(ctx)
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "cancel_confirmation_order")

    llm.register_function("refresh_confirmation_order", refresh_confirmation_order)
    llm.register_function("update_delivery_address", update_delivery_address)
    llm.register_function("update_item_quantity", update_item_quantity)
    llm.register_function("list_item_variants", list_item_variants)
    llm.register_function("swap_item_variant", swap_item_variant)
    llm.register_function("cancel_confirmation_order", cancel_confirmation_order)
