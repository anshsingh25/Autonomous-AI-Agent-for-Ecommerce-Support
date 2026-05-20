"""Line item snapshot for outbound confirmation edits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderLineItemSnapshot:
    """One Shopify line item on the order being confirmed."""

    line_item_id: int
    title: str
    quantity: int
    variant_title: str | None = None
    sku: str | None = None
    product_id: int | None = None
    variant_id: int | None = None

    def spoken_summary(self) -> str:
        label = self.title
        if self.variant_title and self.variant_title.lower() not in label.lower():
            label = f"{label} ({self.variant_title})"
        return f"{label} x{self.quantity}"
