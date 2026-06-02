"""LLM function-calling tools for ecommerce operations."""

from __future__ import annotations

import json
import logging

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams, LLMService

from app.ai.tool_guards import normalize_order_id, reject_tool_without_customer_order_id
from app.ai.tool_hold import speak_tool_hold
from app.ai.tool_parsing import parse_quantity
from app.services.order_service import OrderService, order_service
from app.utils.config import get_settings

logger = logging.getLogger(__name__)


def _inbound_edit_tool_schemas() -> list[FunctionSchema]:
    """Order update tools for inbound support (after customer states order number)."""
    settings = get_settings()
    is_magento = settings.data_source.lower().strip() == "magento"
    zip_label = "UK postcode" if is_magento else "PIN / zip code"
    phone_hint = (
        "Mobile for delivery (UK). Omit to keep existing."
        if is_magento
        else "10-digit India mobile or +91. Omit to keep existing."
    )
    tools = [
        FunctionSchema(
            name="update_delivery_address",
            description=(
                "Update shipping/delivery address on an open order (not yet shipped). "
                "Customer must have stated their order number first. "
                "Get full address in natural speech; read back once and confirm yes before calling. "
                "Say a hold line before calling."
            ),
            properties={
                "order_id": {
                    "type": "string",
                    "description": "Exact order number the customer said. Required.",
                },
                "address1": {
                    "type": "string",
                    "description": "House / street / area line",
                },
                "city": {"type": "string", "description": "City or town"},
                "zip_code": {"type": "string", "description": zip_label},
                "province": {
                    "type": "string",
                    "description": "State or county (optional)",
                },
                "address2": {
                    "type": "string",
                    "description": "Second line / landmark (optional)",
                },
                "name": {
                    "type": "string",
                    "description": "Recipient name on the label (optional)",
                },
                "phone": {
                    "type": "string",
                    "description": phone_hint,
                },
            },
            required=["order_id", "address1", "city", "zip_code"],
        ),
        FunctionSchema(
            name="update_item_quantity",
            description=(
                "Change quantity on a line item (open/unfulfilled orders only). "
                "Confirm the new quantity with the customer first. Say a hold line before calling."
            ),
            properties={
                "order_id": {
                    "type": "string",
                    "description": "Exact order number the customer said. Required.",
                },
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
            required=["order_id", "quantity"],
        ),
    ]
    if not is_magento:
        tools.extend(
            [
                FunctionSchema(
                    name="list_item_variants",
                    description=(
                        "List available sizes/colours in stock for an order line. "
                        "Use before swap when customer asks what is available."
                    ),
                    properties={
                        "order_id": {
                            "type": "string",
                            "description": "Exact order number. Required.",
                        },
                        "product_name": {
                            "type": "string",
                            "description": "Product title (optional if single item)",
                        },
                        "line_index": {
                            "type": "integer",
                            "description": "0-based line index (default 0)",
                        },
                    },
                    required=["order_id"],
                ),
                FunctionSchema(
                    name="swap_item_variant",
                    description=(
                        "Change size, colour, or variant on an open order. "
                        "Confirm with customer first. Say a hold line before calling."
                    ),
                    properties={
                        "order_id": {
                            "type": "string",
                            "description": "Exact order number. Required.",
                        },
                        "new_variant": {
                            "type": "string",
                            "description": "Requested size/colour/variant label",
                        },
                        "product_name": {
                            "type": "string",
                            "description": "Product title if multiple items",
                        },
                        "line_index": {
                            "type": "integer",
                            "description": "0-based line index (default 0)",
                        },
                        "quantity": {
                            "type": "integer",
                            "description": "Qty after swap (optional)",
                        },
                    },
                    required=["order_id", "new_variant"],
                ),
            ]
        )
    return tools


def build_tools_schema() -> ToolsSchema:
    """Tool definitions exposed to Gemini on inbound support."""
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="get_order_status",
                description=(
                    "Look up order status and delivery/shipping address ONLY after the customer "
                    "said their order number aloud. Before calling, tell them to wait while you "
                    "pull up the order. Never guess or use example IDs."
                ),
                properties={
                    "order_id": {
                        "type": "string",
                        "description": "Exact digits the customer said (e.g. 1569). Required.",
                    },
                },
                required=["order_id"],
            ),
            FunctionSchema(
                name="refund_status",
                description=(
                    "Refund status — only after customer stated their order number. "
                    "Tell them you are checking refund status before calling."
                ),
                properties={
                    "order_id": {
                        "type": "string",
                        "description": "Exact digits the customer said. Required.",
                    },
                },
                required=["order_id"],
            ),
            FunctionSchema(
                name="cancel_order",
                description=(
                    "Cancel an order when the customer explicitly requests cancellation. "
                    "Tell them to wait while you process the cancellation before calling."
                ),
                properties={
                    "order_id": {
                        "type": "string",
                        "description": "Order ID to cancel",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Short reason for cancellation",
                    },
                },
                required=["order_id"],
            ),
            FunctionSchema(
                name="track_order",
                description=(
                    "Tracking — only after customer stated their order number. "
                    "Tell them to wait while you check tracking before calling."
                ),
                properties={
                    "order_id": {
                        "type": "string",
                        "description": "Exact digits the customer said. Required.",
                    },
                },
                required=["order_id"],
            ),
            *_inbound_edit_tool_schemas(),
        ]
    )


def register_ecommerce_tools(llm: LLMService, service: OrderService | None = None) -> None:
    """Register async handlers on the LLM service (inbound + browser support)."""
    svc = service or order_service
    data_source = get_settings().data_source.lower().strip()

    async def _guarded_order_id(params: FunctionCallParams) -> tuple[str, dict | None]:
        raw = str(params.arguments.get("order_id", "")).strip()
        order_id = normalize_order_id(raw)
        err = reject_tool_without_customer_order_id(raw, data_source=data_source)
        return order_id, err

    async def _safe_result(params: FunctionCallParams, fn, tool_name: str) -> None:
        try:
            if get_settings().tool_hold_speak_inbound:
                await speak_tool_hold(params)
            await fn(params)
        except Exception:
            logger.exception("Tool %s failed", tool_name)
            await params.result_callback(
                json.dumps(
                    {
                        "found": False,
                        "message": (
                            "I couldn't reach the order system just now. "
                            "Please try again in a moment."
                        ),
                    }
                )
            )

    async def get_order_status(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            logger.info("Tool call: get_order_status(%s)", order_id or "(empty)")
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.get_order_status(order_id)
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "get_order_status")

    async def refund_status(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            logger.info("Tool call: refund_status(%s)", order_id or "(empty)")
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.refund_status(order_id)
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "refund_status")

    async def cancel_order(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            reason = str(p.arguments.get("reason", "")).strip()
            logger.info("Tool call: cancel_order(%s)", order_id or "(empty)")
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.cancel_order(order_id, reason=reason)
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "cancel_order")

    async def track_order(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            logger.info("Tool call: track_order(%s)", order_id or "(empty)")
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.track_order(order_id)
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "track_order")

    async def update_delivery_address(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            logger.info("Tool call: update_delivery_address(%s)", order_id or "(empty)")
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.update_delivery_address(
                order_id,
                address1=str(p.arguments.get("address1", "")),
                city=str(p.arguments.get("city", "")),
                zip_code=str(p.arguments.get("zip_code", "")),
                province=str(p.arguments.get("province", "")),
                address2=str(p.arguments.get("address2", "")),
                name=str(p.arguments.get("name", "")),
                phone=str(p.arguments.get("phone", "")),
            )
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "update_delivery_address")

    async def update_item_quantity(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            logger.info("Tool call: update_item_quantity(%s)", order_id or "(empty)")
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
                                "Please say digit and word, for example: 3, three."
                            ),
                        }
                    )
                )
                return
            result = await svc.update_line_item_quantity(
                order_id,
                quantity=qty,
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
            )
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "update_item_quantity")

    async def list_item_variants(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            if err:
                await p.result_callback(json.dumps(err))
                return
            result = await svc.list_item_variants(
                order_id,
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
            )
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "list_item_variants")

    async def swap_item_variant(params: FunctionCallParams) -> None:
        async def _run(p: FunctionCallParams) -> None:
            order_id, err = await _guarded_order_id(p)
            if err:
                await p.result_callback(json.dumps(err))
                return
            qty_raw = p.arguments.get("quantity")
            quantity = int(qty_raw) if qty_raw is not None else None
            result = await svc.swap_line_item_variant(
                order_id,
                new_variant=str(p.arguments.get("new_variant", "")),
                product_name=str(p.arguments.get("product_name", "")),
                line_index=int(p.arguments.get("line_index", 0)),
                quantity=quantity,
            )
            await p.result_callback(json.dumps(result))

        await _safe_result(params, _run, "swap_item_variant")

    llm.register_function("get_order_status", get_order_status)
    llm.register_function("refund_status", refund_status)
    llm.register_function("cancel_order", cancel_order)
    llm.register_function("track_order", track_order)
    llm.register_function("update_delivery_address", update_delivery_address)
    llm.register_function("update_item_quantity", update_item_quantity)
    if data_source != "magento":
        llm.register_function("list_item_variants", list_item_variants)
        llm.register_function("swap_item_variant", swap_item_variant)
