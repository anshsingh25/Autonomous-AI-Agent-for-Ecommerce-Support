"""Shopify order tag constants for AI confirmation flow."""

TAG_PROCEED = "Proceed"
TAG_AI_CONFIRMED = "AI Confirmed"
TAG_CANCELLED = "Cancelled"
TAG_RETRY_CALL = "Retry Call"
TAG_CALL_IN_PROGRESS = "AI Call In Progress"

# Applied together when the customer confirms on the call.
TAGS_CONFIRMED = (TAG_PROCEED, TAG_AI_CONFIRMED)
TAGS_CANCELLED = (TAG_CANCELLED,)
TAGS_NO_ANSWER = (TAG_RETRY_CALL,)

# Legacy set — prefer ``order_should_skip_auto_call()`` for eligibility checks.
TAGS_SKIP_AUTO_CALL = frozenset(
    {
        TAG_AI_CONFIRMED,
        TAG_CANCELLED,
        TAG_CALL_IN_PROGRESS,
    }
)


def _normalize_tag_set(tags: set[str] | frozenset[str] | list[str]) -> set[str]:
    return {str(t).strip().lower() for t in tags if str(t).strip()}


def order_is_confirmed(tags: set[str] | frozenset[str] | list[str]) -> bool:
    """
    True if the order was already confirmed (no outbound confirmation call needed).

    Matches ``Proceed``, ``AI Confirmed``, or a standalone ``confirmed`` tag (any case).
    """
    lower = _normalize_tag_set(tags)
    if TAG_AI_CONFIRMED.lower() in lower:
        return True
    if "confirmed" in lower:
        return True
    return False


def order_should_skip_auto_call(tags: set[str] | frozenset[str] | list[str]) -> bool:
    """True if we must not place another confirmation call for this order."""
    lower = _normalize_tag_set(tags)
    if order_is_confirmed(tags):
        return True
    if TAG_CANCELLED.lower() in lower:
        return True
    if TAG_CALL_IN_PROGRESS.lower() in lower:
        return True
    return False
