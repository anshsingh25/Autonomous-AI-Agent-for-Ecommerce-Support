"""Detect confirm / cancel intent from customer speech (voice transcription)."""

from __future__ import annotations

import re

from app.models.call_outcome import CallOutcome

# Whole-word / phrase matching (case-insensitive).
_CONFIRM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\byes\b",
        r"\byeah\b",
        r"\byep\b",
        r"\bconfirm(?:ed)?\b",
        r"\bokay\b",
        r"\bok\b",
        r"\bproceed\b",
        r"\bhaan\b",
        r"\bhan\b",
        r"\bh[aā]n\b",
        r"\bcorrect\b",
        r"\bright\b",
        r"\bsahi\b",
        r"\btheek\b",
        r"\bthik\b",
        r"\ball good\b",
        r"\bgo ahead\b",
    )
)

# Customer declined call timing — schedule retry, do NOT cancel the order.
_BAD_TIME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnot now\b",
        r"\bnot a good time\b",
        r"\bbad time\b",
        r"\bwrong time\b",
        r"\bbusy\b",
        r"\bcall (?:me )?later\b",
        r"\bcall back\b",
        r"\bcan'?t talk\b",
        r"\bcannot talk\b",
        r"\bno time\b",
        r"\bbaad mein\b",
        r"\babhi nahi\b",
        r"\bkal\b",
        r"\btomorrow\b",
        r"\bmorning\b",
        r"\bafternoon\b",
        r"\bevening\b",
        r"\bwhatsapp\b",
        r"\bmessage me\b",
    )
)

_CANCEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcancel(?:led|lation)?\b",
        r"\breject(?:ed)?\b",
        r"\bdon'?t want\b",
        r"\bdo not want\b",
        r"\bstop the order\b",
        r"\bdon'?t proceed\b",
        r"\bdon'?t send\b",
        r"\bnot interested\b",
        r"\bwrong order\b",
        r"\bmistake\b",
        r"\bmat bhejo\b",
        r"\bcancel karo\b",
        r"\bnahi chahiye\b",
    )
)

# Order cancellation — explicit phrases only (not bare "no" to timing question).
_STRONG_CANCEL = re.compile(
    (
        r"\b("
        r"cancel|cancelled|reject|don'?t want|do not want|stop the order|"
        r"don'?t proceed|don'?t send|not interested|wrong order|mistake|"
        r"nahi chahiye|cancel karo|mat bhejo"
        r")\b"
    ),
    re.IGNORECASE,
)

# Short "no" / "nahi" only — usually answering "good time?" not "confirm order?"
_BARE_DECLINE = re.compile(
    r"^\s*(no|nope|nah|nahi|nahin|nai|nope)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _has_bad_time_signal(text: str) -> bool:
    return any(p.search(text) for p in _BAD_TIME_PATTERNS)


def detect_confirmation_intent(text: str) -> CallOutcome:
    """
    Classify customer response from combined transcript text.

    "Not a good time" → retry (no_answer), never auto-cancel.
    Bare "no" without order-cancel words → retry, not cancel.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return CallOutcome.NO_ANSWER

    if _has_bad_time_signal(cleaned) or _BARE_DECLINE.match(cleaned):
        return CallOutcome.NO_ANSWER

    cancel_hits = sum(1 for p in _CANCEL_PATTERNS if p.search(cleaned))
    confirm_hits = sum(1 for p in _CONFIRM_PATTERNS if p.search(cleaned))
    has_strong_cancel = bool(_STRONG_CANCEL.search(cleaned))
    has_loose_no = bool(re.search(r"\b(no|nahi|nahin)\b", cleaned, re.IGNORECASE))
    has_loose_yes = bool(re.search(r"\b(yes|haan|okay|ok|confirm)\b", cleaned, re.IGNORECASE))

    if has_loose_no and has_loose_yes:
        return CallOutcome.CANCELLED

    if has_strong_cancel and not confirm_hits:
        return CallOutcome.CANCELLED
    if has_strong_cancel and confirm_hits:
        return CallOutcome.CANCELLED
    if cancel_hits and not confirm_hits:
        return CallOutcome.CANCELLED
    if confirm_hits and not cancel_hits:
        return CallOutcome.CONFIRMED
    if confirm_hits and cancel_hits:
        # Mixed signals — never auto-confirm.
        return CallOutcome.CANCELLED

    # Loose "no" with other words but no explicit cancel phrase.
    if re.search(r"\b(no|nahi|nahin)\b", cleaned, re.IGNORECASE) and not confirm_hits:
        return CallOutcome.NO_ANSWER

    return CallOutcome.UNCLEAR
