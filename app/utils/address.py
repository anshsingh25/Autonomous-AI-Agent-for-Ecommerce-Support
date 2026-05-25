"""Format shipping addresses for voice and tool responses."""

from __future__ import annotations

from typing import Any


def normalize_india_pin(zip_code: str | None) -> str:
    """Strip quotes/spaces from PIN; Shopify rejects values like '394111."""
    raw = str(zip_code or "").strip().strip("'\"")
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits or raw


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

    phone = str(addr.get("phone") or "").strip()
    if phone:
        phone_label = f"Mobile {phone}"
        if phone_label not in parts and phone not in parts:
            parts.append(phone_label)

    return ", ".join(parts) if parts else None


def shipping_address_is_complete(addr: dict[str, Any] | None) -> bool:
    """True when Shopify returned real shipping lines (not PII-redacted stub)."""
    if not addr or isinstance(addr, str):
        return False
    return bool(
        str(addr.get("address1") or "").strip()
        or str(addr.get("phone") or "").strip()
        or str(addr.get("name") or "").strip()
    )


def shipping_address_phone(addr: dict[str, Any] | None) -> str | None:
    """Mobile on the Shopify shipping_address object (Contact / Shipping in Admin)."""
    if not addr or isinstance(addr, str):
        return None
    phone = str(addr.get("phone") or "").strip()
    return phone or None
