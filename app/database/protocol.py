"""Order storage interface — mock or live store API."""

from __future__ import annotations

from typing import Any, Protocol

from app.database.mock_db import Order


class OrderRepository(Protocol):
    async def get_order(self, order_id: str) -> Order | None: ...

    async def update_order(self, order_id: str, **fields: Any) -> Order | None: ...

    async def list_orders(self) -> list[Order]: ...
