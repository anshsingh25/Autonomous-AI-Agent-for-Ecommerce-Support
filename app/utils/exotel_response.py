"""Parse Exotel error bodies (Twilio-compatible XML) and map HTTP status for APIs."""

from __future__ import annotations

import re

_TWILIO_MSG = re.compile(
    r"<Message>\s*([\s\S]*?)\s*</Message>",
    re.IGNORECASE,
)


def exotel_twilio_rest_message(xml_or_text: str | None) -> str | None:
    """Return the human-readable ``<Message>`` from Exotel's XML error body, if present."""
    if not xml_or_text:
        return None
    m = _TWILIO_MSG.search(xml_or_text)
    if not m:
        return None
    return " ".join(m.group(1).split()) or None


def http_proxy_status_for_exotel(exotel_http: int) -> int:
    """Forward Exotel 4xx to our HTTP API; keep 5xx / network issues as 502."""
    if 400 <= exotel_http < 500:
        return exotel_http
    return 502


_exotel_failure = re.compile(
    r"^Exotel connect failed \((\d{3})\):\s*(.+)\Z",
    re.DOTALL,
)


def parse_exotel_connect_error_message(message: str) -> tuple[int | None, str]:
    """If *message* is our ``ValueError`` text from ``_dial_exotel``, return (http_code, body)."""
    m = _exotel_failure.match((message or "").strip())
    if not m:
        return None, message
    code = int(m.group(1))
    tail = (m.group(2) or "").strip()
    inner = exotel_twilio_rest_message(tail)
    detail = inner or tail
    return code, detail
