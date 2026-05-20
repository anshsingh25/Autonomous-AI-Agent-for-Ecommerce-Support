"""Outbound order-confirmation tools only (not used on inbound support)."""

from __future__ import annotations

import json
import logging

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams, LLMService

from app.ai.tool_hold import speak_tool_hold
from app.database.factory import create_order_repository
from app.database.shopify_store import ShopifyRepository
from app.services.call_context_store import get_context
from app.services.outbound_order_edits import (
    apply_address_update,
    apply_quantity_update,
    apply_variant_swap,
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
                    "Reload current order details from Shopify for this confirmation call. "
                    "Use after any change or if you need updated total/address."
                ),
                properties={},
                required=[],
            ),
            FunctionSchema(
                name="update_delivery_address",
                description=(
                    "Update shipping address on this unfulfilled order. "
                    "Collect full address from customer first, read it back, then call. "
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
    if settings.data_source.lower() == "shopify":
        repo = create_order_repository(settings)
        if isinstance(repo, ShopifyRepository):
            shopify = repo

    def _ctx_or_error() -> tuple[Any, dict | None]:
        ctx = get_context(session_id)
        if not ctx:
            return None, {"success": False, "message": "Call session expired"}
        return ctx, None

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
            if not shopify:
                await p.result_callback(
                    json.dumps(
                        {
                            "found": True,
                            "order_id": ctx.order_id,
                            "items": ctx.items,
                            "total_inr": ctx.total_inr,
                            "delivery_address": ctx.delivery_address,
                            "message": "Using cached order (Shopify not configured)",
                        }
                    )
                )
                return
            await refresh_outbound_context(ctx)
            snap = await shopify.get_confirmation_order_snapshot(ctx.order_id)
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
            )
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "update_delivery_address")

    async def update_item_quantity(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            qty = int(p.arguments.get("quantity", 0))
            result = await apply_quantity_update(
                ctx,
                quantity=qty,
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
            )
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
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "swap_item_variant")

    async def cancel_confirmation_order(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            ctx, err = _ctx_or_error()
            if err:
                await p.result_callback(json.dumps(err))
                return
            if not shopify:
                await p.result_callback(
                    json.dumps(
                        {
                            "success": False,
                            "message": "Cancellation requires DATA_SOURCE=shopify",
                        }
                    )
                )
                return
            reason = str(p.arguments.get("reason", "")).strip()
            result = await shopify.cancel_order_api(ctx.order_id, reason=reason)
            await p.result_callback(json.dumps(result))

        await _safe(params, _run, "cancel_confirmation_order")

    llm.register_function("refresh_confirmation_order", refresh_confirmation_order)
    llm.register_function("update_delivery_address", update_delivery_address)
    llm.register_function("update_item_quantity", update_item_quantity)
    llm.register_function("list_item_variants", list_item_variants)
    llm.register_function("swap_item_variant", swap_item_variant)
    llm.register_function("cancel_confirmation_order", cancel_confirmation_order)
