"""Pick Shopify vs Magento confirmation service from DATA_SOURCE (additive)."""

from __future__ import annotations

from typing import Protocol

from app.utils.config import Settings, get_settings


class OrderConfirmationServiceProtocol(Protocol):
    async def dial_order_confirmation(
        self,
        order_id: str,
        *,
        phone: str | None = None,
        force: bool = False,
    ) -> dict: ...

    async def finalize_call(
        self, session_id: str, *, force_no_answer: bool = False
    ) -> dict: ...


def get_order_confirmation_service(
    settings: Settings | None = None,
) -> OrderConfirmationServiceProtocol:
    s = settings or get_settings()
    if s.data_source.lower() == "magento":
        from app.services.magento_order_confirmation_service import (
            magento_order_confirmation_service,
        )

        return magento_order_confirmation_service

    from app.services.order_confirmation_service import order_confirmation_service

    return order_confirmation_service
