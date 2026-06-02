"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for the ecommerce voice assistant."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Ecommerce AI Call Assistant"
    store_brand_name: str = Field(
        default="badlavo",
        validation_alias="STORE_BRAND_NAME",
        description="Spoken brand in greetings (inbound + outbound). Default badlavo for Shopify site.",
    )
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 7860
    log_level: str = "INFO"

    # Public URL for Twilio WebSocket (e.g. wss://abc123.ngrok-free.app/voice/ws)
    public_ws_url: str = Field(default="", validation_alias="PUBLIC_WS_URL")
    # HTTPS base for outbound answer webhooks (defaults from PUBLIC_WS_URL if unset)
    public_http_url: str = Field(default="", validation_alias="PUBLIC_HTTP_URL")

    # Telephony: mock | twilio | exotel (outbound + inbound media stream)
    telephony_provider: str = Field(default="mock", validation_alias="TELEPHONY_PROVIDER")

    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(
        default="models/gemini-2.5-flash-native-audio-preview-12-2025",
        validation_alias="GEMINI_MODEL",
    )
    gemini_voice: str = Field(
        default="Kore",
        validation_alias="GEMINI_VOICE",
        description="Live API voice (e.g. Kore, Aoede, Charon). Kore is clearer for support.",
    )
    gemini_temperature: float = Field(
        default=0.5,
        validation_alias="GEMINI_TEMPERATURE",
        description="Lower = less hallucination; 0.5–0.7 recommended for voice.",
    )
    gemini_max_tokens: int = Field(
        default=1024,
        validation_alias="GEMINI_MAX_TOKENS",
        description="Caps reply length for shorter, clearer voice turns.",
    )
    gemini_outbound_vad_prefix_padding_ms: int = Field(
        default=62,
        validation_alias="GEMINI_OUTBOUND_VAD_PREFIX_PADDING_MS",
        ge=40,
        le=300,
        description="Outbound: ms before speech counts as started.",
    )
    gemini_outbound_vad_silence_duration_ms: int = Field(
        default=110,
        validation_alias="GEMINI_OUTBOUND_VAD_SILENCE_DURATION_MS",
        ge=90,
        le=900,
        description=(
            "Outbound: silence after last word before your turn ends (ms). Lower = faster reply when done; "
            "try 100–130 on phone. Raise if bot replies while customer is still thinking."
        ),
    )
    gemini_outbound_user_turn_stop_timeout_sec: float = Field(
        default=0.1,
        validation_alias="GEMINI_OUTBOUND_USER_TURN_STOP_TIMEOUT_SEC",
        ge=0.05,
        le=0.6,
        description=(
            "Outbound: after Gemini VAD detects silence, wait this long for late STT "
            "before Aria replies. Lower = snappier; raise if replies cut off mid-sentence."
        ),
    )
    gemini_outbound_transcription_debounce_sec: float = Field(
        default=0.03,
        validation_alias="GEMINI_OUTBOUND_TRANSCRIPTION_DEBOUNCE_SEC",
        ge=0.03,
        le=0.4,
        description="Outbound: throttle duplicate STT fragments (seconds).",
    )
    gemini_outbound_transcription_flush_short_sec: float = Field(
        default=0.028,
        validation_alias="GEMINI_OUTBOUND_TRANSCRIPTION_FLUSH_SHORT_SEC",
        ge=0.025,
        le=0.5,
        description="Outbound: STT flush while a short reply is still forming (yes/hindi/etc.).",
    )
    gemini_outbound_transcription_flush_ultra_short_sec: float = Field(
        default=0.025,
        validation_alias="GEMINI_OUTBOUND_TRANSCRIPTION_FLUSH_ULTRA_SHORT_SEC",
        ge=0.02,
        le=0.2,
        description=(
            "Outbound: STT flush when buffer is a complete one-word answer "
            "(yes/no/english/hindi). Longer sentences still use FLUSH_LONG."
        ),
    )
    gemini_outbound_transcription_flush_long_sec: float = Field(
        default=0.038,
        validation_alias="GEMINI_OUTBOUND_TRANSCRIPTION_FLUSH_LONG_SEC",
        ge=0.025,
        le=0.6,
        description="Outbound: STT flush when Gemini streams without sentence boundary — lower = quicker.",
    )
    tool_hold_pause_sec: float = Field(
        default=0.28,
        validation_alias="TOOL_HOLD_PAUSE_SEC",
        ge=0.0,
        le=3.0,
        description=(
            "Seconds after spoken 'hold' line before tool/API runs. "
            "Previously ~0.9s blocked every tool call (heavy dead air on phone)."
        ),
    )
    tool_hold_speak_inbound: bool = Field(
        default=False,
        validation_alias="TOOL_HOLD_SPEAK_INBOUND",
        description=(
            "If true, inject an extra spoken hold line before each inbound tool (often duplicates "
            "what the model already said and sounds mid-call). Leave false; model says hold once per turn."
        ),
    )

    twilio_account_sid: str = Field(default="", validation_alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(default="", validation_alias="TWILIO_AUTH_TOKEN")
    twilio_phone_number: str = Field(default="", validation_alias="TWILIO_PHONE_NUMBER")

    exotel_account_sid: str = Field(default="", validation_alias="EXOTEL_ACCOUNT_SID")
    exotel_api_key: str = Field(default="", validation_alias="EXOTEL_API_KEY")
    exotel_api_token: str = Field(default="", validation_alias="EXOTEL_API_TOKEN")
    exotel_caller_id: str = Field(
        default="",
        validation_alias="EXOTEL_CALLER_ID",
        description="ExoPhone number for outbound CallerId",
    )
    exotel_api_host: str = Field(
        default="https://api.exotel.com",
        validation_alias="EXOTEL_API_HOST",
        description=(
            "Match Exotel **account** region in dashboard (Singapore→api.exotel.com; "
            "India cluster→api.in.exotel.com), not your physical location."
        ),
    )
    exotel_outbound_app_id: str = Field(
        default="",
        validation_alias="EXOTEL_OUTBOUND_APP_ID",
        description="App Bazaar voice flow id (Voicebot applet → dynamic WSS URL)",
    )
    exotel_outbound_flow_url: str = Field(
        default="",
        validation_alias="EXOTEL_OUTBOUND_FLOW_URL",
        description=(
            "ExoML start URL, e.g. http://my.exotel.com/your_account/exoml/start_voice/APP_ID. "
            "Built from EXOTEL_OUTBOUND_APP_ID if empty."
        ),
    )
    exotel_inbound_app_id: str = Field(
        default="",
        validation_alias="EXOTEL_INBOUND_APP_ID",
        description=(
            "App Bazaar flow for inbound support. Falls back to EXOTEL_OUTBOUND_APP_ID if empty."
        ),
    )
    exotel_inbound_flow_url: str = Field(
        default="",
        validation_alias="EXOTEL_INBOUND_FLOW_URL",
        description="ExoML start URL for inbound; built from EXOTEL_INBOUND_APP_ID if empty.",
    )

    # Order data: mock | shopify | magento | api
    data_source: str = Field(default="mock", validation_alias="DATA_SOURCE")

    order_desk_enabled: bool = Field(
        default=True,
        validation_alias="ORDER_DESK_ENABLED",
        description="Read/update customer & shipping from data/order_desk.json (PHP UI)",
    )
    order_desk_json_path: str = Field(
        default="data/order_desk.json",
        validation_alias="ORDER_DESK_JSON_PATH",
    )
    order_desk_sync_secret: str = Field(
        default="",
        validation_alias="ORDER_DESK_SYNC_SECRET",
        description=(
            "Shared secret for POST /api/order-desk/* (PHP post-save, scripts). "
            "Header: X-Order-Desk-Sync-Secret. When unset, sync routes are disabled."
        ),
    )

    shopify_shop_domain: str = Field(default="", validation_alias="SHOPIFY_SHOP_DOMAIN")
    shopify_access_token: str = Field(default="", validation_alias="SHOPIFY_ACCESS_TOKEN")
    shopify_api_version: str = Field(default="2024-10", validation_alias="SHOPIFY_API_VERSION")
    shopify_webhook_secret: str = Field(
        default="",
        validation_alias="SHOPIFY_WEBHOOK_SECRET",
        description="HMAC secret from Shopify webhook (optional in dev)",
    )

    # Magento 2.4 (separate site — set DATA_SOURCE=magento only on that deployment)
    magento_base_url: str = Field(
        default="",
        validation_alias="MAGENTO_BASE_URL",
        description="Store base URL, e.g. https://your-store.com (no /rest suffix)",
    )
    magento_access_token: str = Field(
        default="",
        validation_alias="MAGENTO_ACCESS_TOKEN",
        description="Integration access token (Admin → System → Integrations)",
    )
    magento_store_code: str = Field(
        default="default",
        validation_alias="MAGENTO_STORE_CODE",
        description=(
            "Single REST store view code: /rest/{code}/V1. Use 'all' for admin scope, "
            "or set MAGENTO_STORE_CODES for multiple store views."
        ),
    )
    magento_store_codes: str = Field(
        default="",
        validation_alias="MAGENTO_STORE_CODES",
        description=(
            "Comma-separated store view codes (from Stores → All Stores → Store View). "
            "When set, searches each view for orders. Example: "
            "thamesmead,charlton,ades,manchester,main_wholesale_store_view"
        ),
    )
    magento_webhook_secret: str = Field(
        default="",
        validation_alias="MAGENTO_WEBHOOK_SECRET",
        description=(
            "Shared secret for POST /api/magento/webhooks/order-created. "
            "Header: X-Magento-Webhook-Secret"
        ),
    )
    auto_confirmation_enabled: bool = Field(
        default=True,
        validation_alias="AUTO_CONFIRMATION_ENABLED",
        description="Auto-dial customers for unfulfilled Shopify orders via webhook",
    )
    confirmation_webhook_require_tag: str = Field(
        default="",
        validation_alias="CONFIRMATION_WEBHOOK_REQUIRE_TAG",
        description=(
            "If set, orders/create and orders/paid webhooks only auto-dial when this tag "
            "is on the order (Shopify tags and/or order desk tags). Empty = dial all eligible."
        ),
    )
    store_api_base_url: str = Field(default="", validation_alias="STORE_API_BASE_URL")
    store_api_key: str = Field(default="", validation_alias="STORE_API_KEY")
    store_api_header_name: str = Field(
        default="",
        validation_alias="STORE_API_HEADER_NAME",
        description="Optional. e.g. X-API-Key instead of Bearer",
    )
    store_api_timeout: float = Field(default=30.0, validation_alias="STORE_API_TIMEOUT")
    store_api_path_order: str = Field(
        default="/orders/{order_id}",
        validation_alias="STORE_API_PATH_ORDER",
    )
    store_api_path_refund: str = Field(
        default="/orders/{order_id}/refund",
        validation_alias="STORE_API_PATH_REFUND",
    )
    store_api_path_cancel: str = Field(
        default="/orders/{order_id}/cancel",
        validation_alias="STORE_API_PATH_CANCEL",
    )
    store_api_path_track: str = Field(
        default="/orders/{order_id}/tracking",
        validation_alias="STORE_API_PATH_TRACK",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
