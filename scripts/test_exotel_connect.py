#!/usr/bin/env python3
"""Smoke-test Exotel credentials and Calls/connect (no full call required)."""

from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlencode

import httpx

# Load .env without printing secrets
from pathlib import Path

_env = Path(__file__).resolve().parents[1] / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.split("#", 1)[0].strip()
        os.environ.setdefault(k.strip(), v)


async def main() -> int:
    sid = os.environ.get("EXOTEL_ACCOUNT_SID", "").strip()
    key = os.environ.get("EXOTEL_API_KEY", "").strip()
    token = os.environ.get("EXOTEL_API_TOKEN", "").strip()
    caller = os.environ.get("EXOTEL_CALLER_ID", "").strip()
    host = os.environ.get("EXOTEL_API_HOST", "https://api.in.exotel.com").rstrip("/")
    http_base = os.environ.get("PUBLIC_HTTP_URL", "").strip()
    if not http_base:
        ws = os.environ.get("PUBLIC_WS_URL", "").strip()
        if ws.startswith("wss://"):
            http_base = "https://" + ws[len("wss://") :]
        elif ws.startswith("ws://"):
            http_base = "http://" + ws[len("ws://") :]

    missing = [n for n, v in [
        ("EXOTEL_ACCOUNT_SID", sid),
        ("EXOTEL_API_KEY", key),
        ("EXOTEL_API_TOKEN", token),
        ("EXOTEL_CALLER_ID", caller),
    ] if not v]
    if missing:
        print("FAIL: missing env:", ", ".join(missing))
        return 1

    print("OK: Exotel env vars present")
    print(f"    account={sid} caller_id={caller} host={host}")

    # 1) Account / balance or similar lightweight GET
    url = f"{host}/v1/Accounts/{sid}.json"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, auth=(key, token))
    print(f"GET Account: HTTP {r.status_code}")
    if r.status_code == 200:
        print("OK: Exotel API auth works")
    elif r.status_code == 401:
        print("FAIL: 401 Unauthorized — check API key/token")
        return 1
    else:
        body = (r.text or "")[:300]
        print(f"WARN: unexpected response: {body}")

    # 2) Optional: test connect with dummy session (needs phone arg)
    test_phone = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not test_phone:
        print("SKIP: pass a 10-digit test phone to try Calls/connect, e.g.:")
        print("  python scripts/test_exotel_connect.py 98XXXXXXXX")
        return 0 if r.status_code == 200 else 1

    if not http_base:
        print("FAIL: PUBLIC_HTTP_URL or PUBLIC_WS_URL required for connect Url")
        return 1

    session_id = "test-smoke-session"
    answer_url = (
        f"{http_base.rstrip('/')}/voice/outbound/exotel-answer?"
        f"{urlencode({'session_id': session_id})}"
    )
    connect_url = f"{host}/v1/Accounts/{sid}/Calls/connect"
    params = {
        "From": caller,
        "To": test_phone if test_phone.startswith("+") else f"+91{test_phone}",
        "CallerId": caller,
        "Url": answer_url,
        "Record": "false",
    }
    print(f"POST Calls/connect To={params['To']}")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r2 = await client.post(connect_url, params=params, auth=(key, token))
    print(f"Connect: HTTP {r2.status_code}")
    print((r2.text or "")[:500])
    return 0 if r2.status_code in (200, 201) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
