"""Shared confirmation desk metadata (Shopify vs Magento URLs)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.utils.config import Settings, get_settings

router = APIRouter(prefix="/api/confirmation", tags=["confirmation"])


@router.get("/desk-meta")
async def confirmation_desk_meta(
    settings: Settings = Depends(get_settings),
) -> dict:
    """
    UI config for /browser/confirmation-desk — picks Shopify or Magento API paths.
    """
    brand = (settings.store_brand_name or "badlavo").strip() or "badlavo"
    ds = settings.data_source.lower().strip()

    if ds == "magento":
        return {
            "data_source": "magento",
            "brand": brand,
            "platform_label": "Magento",
            "pending_url": "/api/magento/confirmation/pending",
            "scan_url": "/api/magento/confirmation/scan-open",
            "tagged_pending_url": None,
            "dial_tagged_url": None,
            "supports_tag_batch": False,
        }

    return {
        "data_source": ds or "mock",
        "brand": brand,
        "platform_label": "Shopify" if ds == "shopify" else "store",
        "pending_url": "/api/shopify/confirmation/pending",
        "scan_url": "/api/shopify/confirmation/scan-unfulfilled",
        "tagged_pending_url": "/api/shopify/confirmation/tagged-pending",
        "dial_tagged_url": "/api/shopify/confirmation/dial-tagged",
        "supports_tag_batch": ds == "shopify",
    }
