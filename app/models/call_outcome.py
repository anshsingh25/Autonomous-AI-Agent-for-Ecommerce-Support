"""Outcome of an outbound order-confirmation call."""

from __future__ import annotations

from enum import Enum


class CallOutcome(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    NO_ANSWER = "no_answer"
    UNCLEAR = "unclear"
