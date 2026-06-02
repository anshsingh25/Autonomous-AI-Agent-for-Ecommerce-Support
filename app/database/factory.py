"""Create the order repository from application settings."""

from __future__ import annotations

import logging

from app.database.http_store import HttpStoreRepository
from app.database.magento_store import MagentoRepository
from app.database.mock_db import MockDatabase
from app.database.protocol import OrderRepository
from app.database.shopify_store import ShopifyRepository
from app.utils.config import Settings

logger = logging.getLogger(__name__)


def create_order_repository(settings: Settings) -> OrderRepository:
    source = settings.data_source.lower().strip()
    if source == "shopify":
        if not settings.shopify_shop_domain.strip():
            raise ValueError(
                "DATA_SOURCE=shopify requires SHOPIFY_SHOP_DOMAIN "
                "(e.g. your-store.myshopify.com)"
            )
        repo = ShopifyRepository(settings)
        logger.info("Order data: Shopify store %s", settings.shopify_shop_domain)
        return repo

    if source == "magento":
        if not settings.magento_base_url.strip():
            raise ValueError(
                "DATA_SOURCE=magento requires MAGENTO_BASE_URL "
                "(e.g. https://your-magento-store.com)"
            )
        repo = MagentoRepository(settings)
        logger.info("Order data: Magento at %s", settings.magento_base_url)
        return repo

    if source == "api":
        if not settings.store_api_base_url.strip():
            raise ValueError(
                "DATA_SOURCE=api requires STORE_API_BASE_URL "
                "(e.g. https://your-store.com/api/v1)"
            )
        logger.info("Order data: live store API at %s", settings.store_api_base_url)
        return HttpStoreRepository(settings)

    logger.info("Order data: in-memory mock (set DATA_SOURCE=api for production)")
    return MockDatabase()
