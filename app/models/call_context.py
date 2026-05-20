"""Per-call context for inbound support vs outbound order confirmation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.models.call_outcome import CallOutcome
from app.models.order_line_item import OrderLineItemSnapshot


class CallMode(str, Enum):
    INBOUND_SUPPORT = "inbound_support"
    OUTBOUND_CONFIRMATION = "outbound_confirmation"


@dataclass
class OutboundCallContext:
    """Order facts injected into an outbound confirmation call."""

    session_id: str
    order_id: str
    customer_name: str
    customer_phone: str
    items: list[str]
    total_inr: float
    status: str
    delivery_address: str | None = None
    line_items: list[OrderLineItemSnapshot] = field(default_factory=list)
    shopify_order_id: int | None = None
    shopify_order_gid: str | None = None
    fulfillment_status: str | None = None
    mode: CallMode = CallMode.OUTBOUND_CONFIRMATION
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    provider_call_id: str | None = None
    customer_answered: bool = False
    user_transcripts: list[str] = field(default_factory=list)
    outcome: CallOutcome = CallOutcome.PENDING
    outcome_applied: bool = False

    def summary_for_prompt(self) -> str:
        if self.line_items:
            items_text = "; ".join(li.spoken_summary() for li in self.line_items)
        else:
            items_text = ", ".join(self.items) if self.items else "your items"
        addr = self.delivery_address or "address on file"
        return (
            f"Order {self.order_id} for {self.customer_name}: {items_text}, "
            f"total ₹{self.total_inr:.0f}, status {self.status}. "
            f"Delivery: {addr}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "order_id": self.order_id,
            "customer_name": self.customer_name,
            "customer_phone": self.customer_phone,
            "items": self.items,
            "total_inr": self.total_inr,
            "status": self.status,
            "delivery_address": self.delivery_address,
            "mode": self.mode.value,
        }
