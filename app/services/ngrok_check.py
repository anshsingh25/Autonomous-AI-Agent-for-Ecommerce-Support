"""Detect when PUBLIC_WS_URL does not match the local ngrok tunnel."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from app.utils.config import Settings

logger = logging.getLogger(__name__)


def detect_ngrok_mismatch(settings: Settings) -> dict | None:
    """
    If ngrok is running on :4040, compare its HTTPS URL to PUBLIC_HTTP_URL / PUBLIC_WS_URL.
    Returns a warning dict when hosts differ (common cause of Exotel disconnect on answer).
    """
    configured = (settings.public_http_url or settings.public_ws_url or "").strip()
    if not configured:
        return None

    try:
        with httpx.Client(timeout=1.5) as client:
            r = client.get("http://127.0.0.1:4040/api/tunnels")
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    tunnels = data.get("tunnels") or []
    live_https = ""
    for t in tunnels:
        url = (t.get("public_url") or "").strip()
        if url.startswith("https://"):
            live_https = url.rstrip("/")
            break
    if not live_https:
        return None

    def _host(url: str) -> str:
        u = url.strip()
        if u.startswith("wss://"):
            u = "https://" + u[len("wss://") :]
        elif u.startswith("ws://"):
            u = "http://" + u[len("ws://") :]
        return urlparse(u).netloc.lower()

    cfg_host = _host(configured)
    live_host = _host(live_https)
    if not cfg_host or cfg_host == live_host:
        return None

    return {
        "warning": "PUBLIC_WS_URL host does not match live ngrok tunnel",
        "configured_host": cfg_host,
        "live_ngrok_https": live_https,
        "fix": f"Update .env: PUBLIC_WS_URL=wss://{live_host} and PUBLIC_HTTP_URL={live_https}",
        "exotel_voicebot_url": f"{live_https}/voice/inbound/exotel-answer",
    }
