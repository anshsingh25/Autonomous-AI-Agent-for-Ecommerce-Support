"""FastAPI entrypoint for the ecommerce AI call assistant."""

from contextlib import asynccontextmanager
import logging

# Pipecat RTVI + Gemini Live call NLTK sentence tokenization on user speech.
from app.utils.nltk_bootstrap import ensure_nltk_data

ensure_nltk_data()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes import browser, orders, outbound, shopify_webhooks, voice
from app.utils.config import get_settings
from app.utils.logging import setup_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info("Starting %s on port %s", settings.app_name, settings.port)
    logger.info("Order data source: %s", settings.data_source)
    if settings.data_source.lower() == "shopify":
        logger.info("Shopify shop: %s", settings.shopify_shop_domain)
        try:
            from app.database.shopify_store import ShopifyRepository

            ok, msg = await ShopifyRepository(settings).verify_connection()
            if ok:
                logger.info(msg)
            else:
                logger.error("Shopify setup: %s", msg)
        except Exception as exc:
            logger.error("Shopify connection check failed: %s", exc)
    elif settings.data_source.lower() == "api":
        logger.info("Store API: %s", settings.store_api_base_url)
    if not settings.public_ws_url:
        logger.warning(
            "PUBLIC_WS_URL is not set — Twilio cannot connect until you set your ngrok/WebSocket URL"
        )
    yield
    logger.info("Shutting down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Ecommerce voice agent: inbound support and outbound order confirmation",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(browser.router)
    app.include_router(voice.router)
    app.include_router(outbound.router)
    app.include_router(shopify_webhooks.router)
    app.include_router(orders.router)

    @app.get("/")
    async def root():
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "browser_voice_test": "/browser",
            "browser_outbound_test": "/browser/outbound",
            "confirmation_calls_desk": "/browser/confirmation-desk",
            "voice_webhook_inbound": "/voice/incoming",
            "voice_websocket": "/voice/ws",
            "outbound_confirm_call": "POST /api/outbound/confirm-order",
            "shopify_webhook_orders_paid": "POST /api/shopify/webhooks/orders-paid",
            "shopify_pending_confirmation": "GET /api/shopify/confirmation/pending",
            "shopify_scan_unfulfilled": "POST /api/shopify/confirmation/scan-unfulfilled?dry_run=true",
            "browser_websocket": "/browser/ws",
            "browser_outbound_websocket": "/browser/outbound/ws?session_id=...",
            "sample_orders": ["ORD-1001", "ORD-1002", "ORD-1003"],
        }

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "data_source": settings.data_source,
            "shopify_configured": bool(
                settings.shopify_shop_domain and settings.shopify_access_token
            ),
            "store_api_configured": bool(settings.store_api_base_url),
            "telephony_provider": settings.telephony_provider,
            "telephony_configured": bool(settings.public_ws_url),
            "auto_confirmation_enabled": settings.auto_confirmation_enabled,
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run(
        "app.main:app",
        host=s.host,
        port=s.port,
        reload=s.debug,
    )
