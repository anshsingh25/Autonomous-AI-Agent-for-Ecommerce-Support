"""Custom Pipecat frames for ecommerce voice flows."""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.frames.frames import DataFrame


@dataclass
class InboundTypedOrderFrame(DataFrame):
    """Customer typed an order number (browser/DTMF) — do not trigger a new LLM turn."""

    session_id: str
    order_id: str
