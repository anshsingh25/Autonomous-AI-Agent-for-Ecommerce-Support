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
        default="https://api.in.exotel.com",
        validation_alias="EXOTEL_API_HOST",
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

    # Order data: mock | shopify | api
    data_source: str = Field(default="mock", validation_alias="DATA_SOURCE")

    shopify_shop_domain: str = Field(default="", validation_alias="SHOPIFY_SHOP_DOMAIN")
    shopify_access_token: str = Field(default="", validation_alias="SHOPIFY_ACCESS_TOKEN")
    shopify_api_version: str = Field(default="2024-10", validation_alias="SHOPIFY_API_VERSION")
    shopify_webhook_secret: str = Field(
        default="",
        validation_alias="SHOPIFY_WEBHOOK_SECRET",
        description="HMAC secret from Shopify webhook (optional in dev)",
    )
    auto_confirmation_enabled: bool = Field(
        default=True,
        validation_alias="AUTO_CONFIRMATION_ENABLED",
        description="Auto-dial customers for unfulfilled Shopify orders via webhook",
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
