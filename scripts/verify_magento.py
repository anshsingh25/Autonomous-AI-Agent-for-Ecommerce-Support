#!/usr/bin/env python3
"""Verify Magento REST credentials (run on Magento deployment only)."""

from __future__ import annotations

import asyncio
import sys

from app.database.magento_store import MagentoRepository, parse_magento_store_codes
from app.utils.config import get_settings


async def main() -> int:
    settings = get_settings()
    if settings.data_source.lower() != "magento":
        print("Set DATA_SOURCE=magento in .env for this script.")
        return 1

    codes = parse_magento_store_codes(settings)
    print(f"MAGENTO_BASE_URL={settings.magento_base_url.rstrip('/')}")
    print(f"Store scope(s) ({len(codes)}): {', '.join(codes)}")
    if len(codes) == 1 and "," in (settings.magento_store_code or ""):
        print(
            "Tip: use MAGENTO_STORE_CODES=thamesmead,ades "
            "(not a comma inside MAGENTO_STORE_CODE unless you want a single code)."
        )

    repo = MagentoRepository(settings)
    for code in codes:
        ok, via = await repo._probe_store_scope(code)
        status = f"OK ({via})" if ok else "FAILED"
        print(f"  [{code}] {status}")

    ok, msg = await repo.verify_connection()
    print(msg)
    if not ok:
        return 1

    sample = await repo.list_recent_unfulfilled_orders(limit=3)
    print(f"Sample open orders (all scopes): {len(sample)}")
    for o in sample:
        print(
            f"  - {o.get('increment_id')} status={o.get('status')} "
            f"total={o.get('grand_total')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
