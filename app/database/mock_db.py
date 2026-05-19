"""In-memory mock ecommerce database for demos and development."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class Order:
    order_id: str
    customer_name: str
    phone: str
    status: str
    total_inr: float
    items: list[str]
    placed_at: datetime
    tracking_id: str | None = None
    carrier: str | None = None
    refund_status: str | None = None
    cancel_allowed: bool = True
    delivery_address: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Sample orders — replace with your real database later.
ORDERS: dict[str, Order] = {
    "ORD-1001": Order(
        order_id="ORD-1001",
        customer_name="Rahul Sharma",
        phone="+919876543210",
        status="shipped",
        total_inr=2499.0,
        items=["Wireless Earbuds", "Phone Case"],
        placed_at=_utc_now() - timedelta(days=2),
        tracking_id="TRK-IN-88421",
        carrier="BlueDart",
        refund_status="not_requested",
        delivery_address=(
            "Rahul Sharma, 42 MG Road, Koramangala, Bengaluru, Karnataka 560034, India"
        ),
    ),
    "ORD-1002": Order(
        order_id="ORD-1002",
        customer_name="Priya Patel",
        phone="+919123456789",
        status="processing",
        total_inr=1599.0,
        items=["Cotton Kurta - Blue"],
        placed_at=_utc_now() - timedelta(hours=6),
        tracking_id=None,
        carrier=None,
        refund_status="not_requested",
        delivery_address="Priya Patel, 18 Park Street, Ahmedabad, Gujarat 380006, India",
    ),
    "ORD-1003": Order(
        order_id="ORD-1003",
        customer_name="John Miller",
        phone="+15551234567",
        status="delivered",
        total_inr=4999.0,
        items=["Smart Watch Series 5"],
        placed_at=_utc_now() - timedelta(days=10),
        tracking_id="TRK-IN-77102",
        carrier="Delhivery",
        refund_status="completed",
        cancel_allowed=False,
        delivery_address="John Miller, 7 Marine Drive, Mumbai, Maharashtra 400002, India",
    ),
}


class MockDatabase:
    """Async-friendly mock store (in-memory)."""

    def __init__(self, orders: dict[str, Order] | None = None) -> None:
        self._orders: dict[str, Order] = dict(orders or ORDERS)

    async def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id.upper())

    async def update_order(self, order_id: str, **fields: Any) -> Order | None:
        order = await self.get_order(order_id)
        if not order:
            return None
        for key, value in fields.items():
            if hasattr(order, key):
                setattr(order, key, value)
        return order

    async def list_orders(self) -> list[Order]:
        return list(self._orders.values())


# Shared singleton for the app
db = MockDatabase()
