"""Format shipping addresses for voice and tool responses."""

from __future__ import annotations

from typing import Any


def format_delivery_address(addr: dict[str, Any] | None) -> str | None:
    """Build a single spoken line from Shopify-style address fields."""
    if not addr:
        return None

    if isinstance(addr, str):
        text = addr.strip()
        return text or None

    parts: list[str] = []
    for key in (
        "name",
        "address1",
        "address2",
        "city",
        "province",
        "province_code",
        "zip",
        "country",
    ):
        value = addr.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in parts:
            parts.append(text)

    return ", ".join(parts) if parts else None
