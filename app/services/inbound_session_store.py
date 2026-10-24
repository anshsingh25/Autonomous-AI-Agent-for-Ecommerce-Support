"""Per-call storage for customer-typed order numbers (browser / future DTMF)."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

_TTL_SEC = 7200
_store: dict[str, dict[str, Any]] = {}
_lock = Lock()


def set_typed_order_id(session_id: str, order_id: str) -> str:
    """Save normalized order id the customer typed; returns normalized value."""
    from app.ai.tool_guards import normalize_order_id

    key = (session_id or "").strip()
    if not key:
        return ""
    cleaned = normalize_order_id(order_id)
    if not cleaned:
        return ""
    with _lock:
        _store[key] = {
            "typed_order_id": cleaned,
            "updated_at": time.time(),
        }
    return cleaned


def get_typed_order_id(session_id: str | None) -> str | None:
    if not session_id:
        return None
    key = session_id.strip()
    with _lock:
        row = _store.get(key)
        if not row:
            return None
        if time.time() - float(row.get("updated_at", 0)) > _TTL_SEC:
            _store.pop(key, None)
            return None
        val = str(row.get("typed_order_id") or "").strip()
        return val or None


def clear_session(session_id: str | None) -> None:
    if not session_id:
        return
    with _lock:
        _store.pop(session_id.strip(), None)
