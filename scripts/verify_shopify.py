#!/usr/bin/env python3
"""Verify Shopify domain, token, and order lookup. Usage: python scripts/verify_shopify.py 1569"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from app.database.shopify_store import ShopifyRepository
from app.utils.config import get_settings


async def main() -> None:
    settings = get_settings()
    repo = ShopifyRepository(settings)

    ok, msg = await repo.verify_connection()
    print(msg)
    if not ok:
        print(
            "\nHow to fix SHOPIFY_SHOP_DOMAIN:\n"
            "  1. In Shopify Admin, open Settings → Domains\n"
            "  2. Copy the address ending in .myshopify.com (not badlovo.com)\n"
            "  3. Or copy the store handle from the admin URL:\n"
            "     admin.shopify.com/store/YOUR-HANDLE/orders → YOUR-HANDLE.myshopify.com\n"
        )
        sys.exit(1)

    order_id = sys.argv[1] if len(sys.argv) > 1 else "1569"
    order = await repo.get_order(order_id)
    if order:
        print(f"Order #{order_id} found: status={order.status}, total_inr={order.total_inr}")
    else:
        print(f"Order #{order_id} not found (connection works — check order number).")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
