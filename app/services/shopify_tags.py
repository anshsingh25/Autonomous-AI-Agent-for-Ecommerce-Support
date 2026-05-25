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


def normalize_tag_label(tag: str | None) -> str:
    """Lowercase trimmed tag for comparisons (matches Shopify desk behavior)."""
    return str(tag or "").strip().lower()


def _parse_shopify_csv_tags(tags_value: str | None) -> list[str]:
    """Same as ShopifyRepository.parse_tags but avoids importing shopify_store."""
    return [t.strip() for t in (tags_value or "").split(",") if t.strip()]


def merged_visible_tags(
    shopify_tags_csv: str | None,
    desk_tags: list[str] | None,
) -> list[str]:
    """
    Combined tag list from Shopify ``tags`` string and order desk ``tags`` array.

    Deduplicates case-insensitively while keeping a readable representative casing
    from the first occurrence.
    """
    shop = _parse_shopify_csv_tags(shopify_tags_csv)
    desk = [str(t).strip() for t in (desk_tags or []) if str(t).strip()]
    seen: set[str] = set()
    out: list[str] = []
    for t in shop + desk:
        key = normalize_tag_label(t)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(t.strip())
    return out


def order_has_trigger_tag(
    shopify_tags_csv: str | None,
    desk_tags: list[str] | None,
    required: str | None,
) -> bool:
    """
    True if ``required`` is empty (no filter) or present on merged Shopify + desk tags.
    Used for webhook gating and batch dial-by-tag APIs.
    """
    req = (required or "").strip()
    if not req:
        return True
    need = normalize_tag_label(req)
    for t in merged_visible_tags(shopify_tags_csv, desk_tags):
        if normalize_tag_label(t) == need:
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
