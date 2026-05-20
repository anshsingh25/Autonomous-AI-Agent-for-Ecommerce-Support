"""LLM function-calling tools for ecommerce operations."""

from __future__ import annotations

import json
import logging

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams, LLMService

from app.ai.tool_guards import normalize_order_id, reject_tool_without_customer_order_id
from app.ai.tool_hold import speak_tool_hold
from app.services.order_service import OrderService, order_service

logger = logging.getLogger(__name__)


def build_tools_schema() -> ToolsSchema:
    """Tool definitions exposed to Gemini."""
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
        ]
    )


def register_ecommerce_tools(llm: LLMService, service: OrderService | None = None) -> None:
    """Register async handlers on the LLM service."""
    svc = service or order_service

    async def _guarded_order_id(params: FunctionCallParams) -> tuple[str, dict | None]:
        raw = str(params.arguments.get("order_id", "")).strip()
        order_id = normalize_order_id(raw)
        err = reject_tool_without_customer_order_id(raw)
        return order_id, err

    async def _safe_result(params: FunctionCallParams, fn, tool_name: str) -> None:
        try:
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

    llm.register_function("get_order_status", get_order_status)
    llm.register_function("refund_status", refund_status)
    llm.register_function("cancel_order", cancel_order)
    llm.register_function("track_order", track_order)
