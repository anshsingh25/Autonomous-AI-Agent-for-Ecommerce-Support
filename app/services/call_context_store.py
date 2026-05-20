"""In-memory store for outbound call sessions (use Redis in production)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.models.call_context import OutboundCallContext

logger = logging.getLogger(__name__)

_TTL = timedelta(hours=2)
_sessions: dict[str, OutboundCallContext] = {}


def save_context(ctx: OutboundCallContext) -> None:
    _sessions[ctx.session_id] = ctx
    logger.debug("Saved call context session_id=%s order=%s", ctx.session_id, ctx.order_id)


def get_context(session_id: str) -> OutboundCallContext | None:
    ctx = _sessions.get(session_id)
    if not ctx:
        return None
    if datetime.now(timezone.utc) - ctx.created_at > _TTL:
        _sessions.pop(session_id, None)
        return None
    return ctx


def attach_provider_call_id(session_id: str, provider_call_id: str) -> None:
    ctx = get_context(session_id)
    if ctx:
        ctx.provider_call_id = provider_call_id
        logger.debug("Attached call_id=%s to session=%s", provider_call_id, session_id)
