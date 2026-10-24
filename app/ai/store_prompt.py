"""Platform-aware prompts (Shopify site unchanged when DATA_SOURCE=shopify)."""

from __future__ import annotations

from app.ai.prompts_outbound_uk import OUTBOUND_UK_MAGENTO_RULES
from app.utils.config import Settings


def is_uk_voice_site(settings: Settings) -> bool:
    """UK English voice flows (Magento deployment — no Hindi / language pick)."""
    return settings.data_source.lower().strip() == "magento"


def store_brand(settings: Settings) -> str:
    name = (settings.store_brand_name or "badlavo").strip()
    return name or "badlavo"


def order_platform_label(settings: Settings) -> str:
    ds = settings.data_source.lower().strip()
    if ds == "magento":
        return "Magento"
    if ds == "shopify":
        return "Shopify"
    return "store"


def _browser_order_number_rules(settings: Settings) -> str:
    """Voice chat has an on-screen order number field — prefer typing over verbal ID questions."""
    if settings.data_source.lower().strip() == "magento":
        return (
            "\n\nORDER NUMBER — USE THE ON-SCREEN BOX (mandatory wording): "
            "The customer sees an order number field on this page (below Start conversation). "
            "When you need their order number, tell them once to TYPE it in that box and tap Send — "
            'e.g. "Please type your order number in the box on screen and tap Send — I\'ll look it up." '
            'Do NOT ask "What is your order ID?", "What\'s your order number?", or similar as your main ask. '
            "When they tap Send, the system looks up that order automatically — speak the full result. "
            "If they say a different number aloud later, prefer what they said. "
            "Do not interrupt the call when they type."
        )
    return (
        "\n\nORDER NUMBER — ON-SCREEN BOX: When you need their order number on this browser chat, "
        "ask them to type it in the order number box on screen and tap Send — not only "
        '"What is your order number?" They may also say digits aloud. '
        "Use the typed number from the system note when you receive it."
    )


def build_inbound_opening_greeting(settings: Settings) -> str:
    brand = store_brand(settings)
    return (
        f"Hey there! Thanks for calling {brand}. This is Aria. How can I help you today?"
    )


def build_inbound_system_instruction(settings: Settings) -> str:
    """Inbound support prompt — identical to legacy Shopify/badlavo unless DATA_SOURCE=magento."""
    from app.ai.prompts import SYSTEM_INSTRUCTION

    typed_order_note = _browser_order_number_rules(settings)

    if settings.data_source.lower() != "magento":
        brand = store_brand(settings)
        if brand.lower() == "badlavo":
            return SYSTEM_INSTRUCTION + typed_order_note
        return SYSTEM_INSTRUCTION.replace("badlavo", brand) + typed_order_note

    brand = store_brand(settings)
    text = SYSTEM_INSTRUCTION.replace("badlavo", brand)
    text = text.replace("Shopify shipping address", "Magento shipping address")
    text = text.replace(
        "this is the Shopify shipping address",
        "this is the Magento shipping address",
    )
    text = text.replace(
        "Read delivery_address from the tool result — this is the Shopify shipping address",
        "Read delivery_address from the tool result — this is the Magento shipping address",
    )
    if is_uk_voice_site(settings):
        text = text.replace(
            "If they say hello, hi, hey, namaste, or speak in Hindi:",
            "If they say hello, hi, or hey:",
        )
        text += (
            "\n\nUK ENGLISH: Use British English only. Never ask which language they prefer "
            "and do not offer Hindi."
        )
        text += (
            "\n\nINBOUND ORDER EDITS (Magento): After the customer gives their order number, "
            "you may update_delivery_address and update_item_quantity on open orders — "
            "changes save to Magento Admin (requires Aria_VoiceOrder module on the store). "
            "Use postcode not PIN. Variant swap tools are not available on Magento."
        )
    return text + typed_order_note


def build_outbound_system_instruction(settings: Settings) -> str:
    from app.ai.prompts_outbound import (
        build_outbound_system_instruction as _build_base,
    )

    text = _build_base()
    brand = store_brand(settings)
    if brand.lower() != "badlavo":
        text = text.replace("badlavo", brand)
    if settings.data_source.lower() == "magento":
        text = text.replace(
            "from the order desk (Shopify export imported into the desk UI), "
            "not live Shopify PII API",
            "from the order desk (Magento export or manual desk entry), "
            "with Magento REST when configured",
        )
    if is_uk_voice_site(settings):
        text = _apply_uk_outbound_overrides(text, brand)
        text = f"{text}\n\n{OUTBOUND_UK_MAGENTO_RULES.format(brand=brand)}"
    return text


def _apply_uk_outbound_overrides(text: str, brand: str) -> str:
    """Strip India/Hindi opening flow from shared base (Magento only)."""
    _ = brand
    pairs = [
        (
            "OPENING: Say intro + language question. No \"Am I speaking with [name]?\".",
            "OPENING: Say intro + ask if they have a minute to confirm (UK English). "
            "Never ask language preference. No \"Am I speaking with [name]?\".",
        ),
        (
            "4 PHASES: (1) intro + language + good time",
            "4 PHASES: (1) intro + good time",
        ),
        (
            'saying "Hi this is Aria from badlavo, which language do you prefer?" again.',
            "replaying the opening or asking language preference again.",
        ),
        (
            "city, state, PIN — not city alone",
            "town/city, county, postcode — not city alone",
        ),
        (
            "house, area, city, and PIN",
            "house, area, town or city, and postcode",
        ),
        (
            "city + PIN",
            "town/city + postcode",
        ),
        (
            "PIN [pin]",
            "postcode [postcode]",
        ),
        (
            "- Hindi: use simple Hindi for confirm question; haan/theek hai = yes.\n",
            "",
        ),
    ]
    for old, new in pairs:
        text = text.replace(old, new)
    return text


def build_outbound_opening_intro(settings: Settings) -> str:
    brand = store_brand(settings)
    return f"Hi! This is Aria calling from {brand}."


def build_outbound_opening(settings: Settings) -> str:
    intro = build_outbound_opening_intro(settings)
    if is_uk_voice_site(settings):
        return (
            f"{intro} I'm calling about your recent order — "
            "do you have a quick minute to confirm the details before we dispatch?"
        )
    from app.ai.prompts_outbound import ORDER_CONFIRMATION_OPENING_LANGUAGE

    return f"{intro} {ORDER_CONFIRMATION_OPENING_LANGUAGE}"


def outbound_bootstrap_message(
    settings: Settings,
    ctx_summary: str,
    customer_name: str,
) -> str:
    opening = build_outbound_opening(settings)
    if is_uk_voice_site(settings):
        return (
            "[System — not the customer] Outbound confirmation call connected (UK English). "
            f'Say ONLY this opening verbatim at normal conversational speed, then stop and listen: '
            f'"{opening}" '
            f"Customer name: {customer_name}. Order facts (do NOT read until they agree they have time): "
            f"{ctx_summary} "
            "British English only — never ask Hindi vs English. "
            "If they say yes / sure / okay / one minute — continue with order recap. "
            "If not now or busy — offer callback, end politely. "
            "Never silence-check right after they spoke."
        )
    return (
        "[System — not the customer] Outbound confirmation call connected. "
        f'Say ONLY this opening verbatim at normal conversational speed, then stop and listen: '
        f'"{opening}" '
        f"Customer name: {customer_name}. Order facts (do NOT read until they have time): {ctx_summary} "
        "When they speak — even one word hindi, english, yes, no, haan, nahi — reply immediately in that language. "
        "After english: short minute-ask in English. After hindi: short minute-ask in Hindi. "
        "Not now → callback. Never silence-check right after they spoke."
    )
