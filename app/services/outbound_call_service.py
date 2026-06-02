"""Place outbound order-confirmation calls via Twilio or Exotel."""

from __future__ import annotations

import logging
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from app.database.factory import create_order_repository
from app.database.magento_store import MagentoRepository
from app.database.shopify_store import ShopifyRepository
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import save_context
from app.services.order_desk_store import (
    full_delivery_address,
    get_order_desk_store,
    order_desk_primary,
)
from app.services.order_service import OrderService, order_service
from app.models.order_line_item import OrderLineItemSnapshot
from app.utils.config import Settings, get_settings
from app.utils.exotel_response import exotel_twilio_rest_message

logger = logging.getLogger(__name__)


def _format_exotel_from(phone_e164: str) -> str:
    """
    Exotel ``From`` for outbound: customer number with leading 0 (India mobile).

    See Exotel Connect API — mobile numbers use 0 + 10 digits (e.g. 09876543210).
    """
    digits = "".join(c for c in phone_e164 if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return "0" + digits[2:]
    if len(digits) == 10:
        return "0" + digits
    if digits.startswith("0") and len(digits) == 11:
        return digits
    return digits


def _normalize_phone_e164(phone: str, *, default_country: str = "91") -> str:
    """Best-effort E.164 for India (+91) and existing + prefixes."""
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        raise ValueError("Customer phone number is empty")
    if phone.strip().startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if digits.startswith(default_country) and len(digits) >= 12:
        return "+" + digits
    return "+" + digits


def _public_http_base(settings: Settings) -> str:
    """HTTPS base URL for telephony webhooks (from PUBLIC_WS_URL or PUBLIC_HTTP_URL)."""
    explicit = getattr(settings, "public_http_url", "") or ""
    if explicit.strip():
        return explicit.strip().rstrip("/")
    ws = settings.public_ws_url.strip()
    if not ws:
        raise ValueError(
            "Set PUBLIC_HTTP_URL or PUBLIC_WS_URL (e.g. https://abc.ngrok-free.app) for outbound webhooks"
        )
    if ws.startswith("wss://"):
        return "https://" + ws[len("wss://") :]
    if ws.startswith("ws://"):
        return "http://" + ws[len("ws://") :]
    if ws.startswith("https://") or ws.startswith("http://"):
        return ws.rstrip("/")
    return "https://" + ws.rstrip("/")


class OutboundCallService:
    def __init__(
        self,
        settings: Settings | None = None,
        orders: OrderService | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._orders = orders or order_service

    def _exotel_outbound_flow_url(self) -> str:
        """
        Exotel Calls/connect requires an ExoML app URL, not a raw WebSocket URL.

        App Bazaar flow must include a Voicebot applet with dynamic HTTPS URL:
        https://YOUR_HOST/voice/outbound/exotel-answer
        (session_id is passed via CustomField on the connect API).
        """
        explicit = getattr(self._settings, "exotel_outbound_flow_url", "").strip()
        if explicit:
            return explicit
        app_id = getattr(self._settings, "exotel_outbound_app_id", "").strip()
        account = self._settings.exotel_account_sid.strip()
        if app_id and account:
            return f"http://my.exotel.com/{account}/exoml/start_voice/{app_id}"
        raise ValueError(
            "Set EXOTEL_OUTBOUND_APP_ID (or EXOTEL_OUTBOUND_FLOW_URL) in .env. "
            "Create an Exotel App Bazaar flow with a Voicebot applet whose dynamic URL is "
            f"{_public_http_base(self._settings)}/voice/outbound/exotel-answer — "
            "see docs/EXOTEL_OUTBOUND_SETUP.md"
        )

    def _exotel_inbound_flow_url(self) -> str:
        """
        ExoML app for inbound support (Calls/connect ``Url`` and ExoPhone incoming).

        Uses EXOTEL_INBOUND_APP_ID when set; otherwise same app as outbound.
        Voicebot dynamic URL: /voice/inbound/exotel-answer or /voice/outbound/exotel-answer
        (outbound handler falls back to inbound WSS when CustomField is empty).
        """
        explicit = getattr(self._settings, "exotel_inbound_flow_url", "").strip()
        if explicit:
            return explicit
        app_id = (
            getattr(self._settings, "exotel_inbound_app_id", "").strip()
            or getattr(self._settings, "exotel_outbound_app_id", "").strip()
        )
        account = self._settings.exotel_account_sid.strip()
        if app_id and account:
            return f"http://my.exotel.com/{account}/exoml/start_voice/{app_id}"
        raise ValueError(
            "Set EXOTEL_INBOUND_APP_ID or EXOTEL_OUTBOUND_APP_ID (or EXOTEL_INBOUND_FLOW_URL). "
            f"Voicebot dynamic URL: {_public_http_base(self._settings)}/voice/inbound/exotel-answer"
        )

    async def build_context(
        self,
        order_id: str,
        *,
        phone: str | None = None,
    ) -> OutboundCallContext:
        result = await self._orders.get_order_status(order_id)
        if not result.get("found"):
            raise ValueError(result.get("message", f"Order {order_id} not found"))

        customer_phone = (phone or "").strip()
        if not customer_phone:
            raw = await self._orders.get_customer_phone(order_id)
            customer_phone = raw or ""
        if not customer_phone:
            raise ValueError(
                f"No phone number on order {order_id}. Pass 'phone' in the API body."
            )

        session_id = str(uuid.uuid4())
        line_items: list[OrderLineItemSnapshot] = []
        shopify_order_id = None

        if order_desk_primary():
            desk_rec = get_order_desk_store().get(str(result["order_id"]))
            if desk_rec:
                shopify_order_id = desk_rec.get("shopify_order_id")
                for li in desk_rec.get("line_items") or []:
                    if not isinstance(li, dict):
                        continue
                    line_items.append(
                        OrderLineItemSnapshot(
                            line_item_id=int(li.get("line_item_id") or 0),
                            title=str(li.get("title") or "Item"),
                            quantity=int(li.get("quantity") or 1),
                            variant_title=li.get("variant_title"),
                            sku=li.get("sku"),
                            product_id=li.get("product_id"),
                            variant_id=li.get("variant_id"),
                        )
                    )

        settings = get_settings()
        ds = settings.data_source.lower()
        if not line_items and ds in ("shopify", "magento"):
            repo = create_order_repository(settings)
            if isinstance(repo, (ShopifyRepository, MagentoRepository)):
                raw = await repo.get_raw_order(str(result["order_id"]))
                if raw:
                    entity = raw.get("id") or raw.get("entity_id")
                    if entity:
                        shopify_order_id = int(entity)
                    line_items = repo.line_items_from_raw(raw)

        delivery_address = result.get("delivery_address")
        if order_desk_primary():
            desk_rec = get_order_desk_store().get(str(result["order_id"]))
            if desk_rec:
                full_addr = full_delivery_address(desk_rec)
                if full_addr:
                    delivery_address = full_addr

        ctx = OutboundCallContext(
            session_id=session_id,
            order_id=str(result["order_id"]),
            customer_name=str(result.get("customer_name") or "there"),
            customer_phone=_normalize_phone_e164(customer_phone),
            items=list(result.get("items") or []),
            line_items=line_items,
            total_inr=float(result.get("total_inr") or 0),
            status=str(result.get("status") or "processing"),
            delivery_address=delivery_address,
            shopify_order_id=shopify_order_id,
            language="en" if self._settings.data_source.lower() == "magento" else None,
        )
        save_context(ctx)
        return ctx

    async def prepare_browser_session(
        self,
        order_id: str,
        *,
        phone: str | None = None,
    ) -> dict[str, Any]:
        """Create outbound call context for browser testing (no phone dial)."""
        ctx = await self.build_context(order_id, phone=phone or "+910000000000")
        return {
            "session_id": ctx.session_id,
            "order_id": ctx.order_id,
            "customer_name": ctx.customer_name,
            "items": ctx.items,
            "total_inr": ctx.total_inr,
            "delivery_address": ctx.delivery_address,
            "status": ctx.status,
            "mode": "browser_simulation",
        }

    async def dial_confirmation_call(
        self,
        order_id: str,
        *,
        phone: str | None = None,
    ) -> dict[str, Any]:
        ctx = await self.build_context(order_id, phone=phone)
        provider = self._settings.telephony_provider.lower().strip()

        if provider == "mock":
            logger.info(
                "MOCK outbound call session=%s to %s order=%s",
                ctx.session_id,
                ctx.customer_phone,
                ctx.order_id,
            )
            return {
                "status": "mock_scheduled",
                "session_id": ctx.session_id,
                "order_id": ctx.order_id,
                "to": ctx.customer_phone,
                "message": (
                    "Mock mode: no call placed. Use session_id in voice test or configure "
                    "TELEPHONY_PROVIDER=twilio or exotel."
                ),
            }

        if provider == "exotel":
            call_id = await self._dial_exotel(ctx)
        elif provider == "twilio":
            call_id = await self._dial_twilio(ctx)
        else:
            raise ValueError(f"Unsupported TELEPHONY_PROVIDER: {provider}")

        return {
            "status": "dialing",
            "session_id": ctx.session_id,
            "order_id": ctx.order_id,
            "to": ctx.customer_phone,
            "provider": provider,
            "provider_call_id": call_id,
        }

    async def _dial_twilio(self, ctx: OutboundCallContext) -> str:
        sid = self._settings.twilio_account_sid.strip()
        token = self._settings.twilio_auth_token.strip()
        from_num = self._settings.twilio_phone_number.strip()
        if not all([sid, token, from_num]):
            raise ValueError("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER required")

        base = _public_http_base(self._settings)
        answer_url = (
            f"{base}/voice/outbound/answer?{urlencode({'session_id': ctx.session_id})}"
        )
        status_url = (
            f"{base}/voice/outbound/status?{urlencode({'session_id': ctx.session_id})}"
        )
        data = {
            "To": ctx.customer_phone,
            "From": from_num,
            "Url": answer_url,
            "Method": "POST",
            "StatusCallback": status_url,
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "StatusCallbackMethod": "POST",
        }
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, data=data, auth=(sid, token))
            response.raise_for_status()
            payload = response.json()
        call_sid = payload.get("sid", "")
        logger.info("Twilio outbound call %s → %s session=%s", call_sid, ctx.customer_phone, ctx.session_id)
        return call_sid

    async def _dial_exotel(self, ctx: OutboundCallContext) -> str:
        sid = self._settings.exotel_account_sid.strip()
        token = self._settings.exotel_api_token.strip()
        api_key = self._settings.exotel_api_key.strip()
        from_num = self._settings.exotel_caller_id.strip()
        if not all([sid, token, api_key, from_num]):
            raise ValueError(
                "EXOTEL_ACCOUNT_SID, EXOTEL_API_TOKEN, EXOTEL_API_KEY, EXOTEL_CALLER_ID required"
            )

        connect_url = self._exotel_outbound_flow_url()
        customer_from = _format_exotel_from(ctx.customer_phone)
        params = {
            "From": customer_from,
            "CallerId": from_num,
            "CallType": "trans",
            "Url": connect_url,
            "CustomField": ctx.session_id,
        }
        host = self._settings.exotel_api_host.rstrip("/")
        api_url = f"{host}/v1/Accounts/{sid}/Calls/connect"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                api_url,
                params=params,
                auth=(api_key, token),
            )
            if response.status_code >= 400:
                raw = (response.text or "")[:2000]
                short = exotel_twilio_rest_message(raw)
                detail = short or raw[:500]
                logger.error(
                    "Exotel connect failed %s: %s",
                    response.status_code,
                    detail,
                )
                raise ValueError(
                    f"Exotel connect failed ({response.status_code}): {detail}"
                )
            text = response.text
        logger.info(
            "Exotel outbound connect session=%s From=%s CallerId=%s Url=%s",
            ctx.session_id,
            customer_from,
            from_num,
            connect_url,
        )
        return text[:120] if text else "exotel-queued"


def exotel_outbound_flow_url(settings: Settings | None = None) -> str:
    return OutboundCallService(settings)._exotel_outbound_flow_url()


def exotel_inbound_flow_url(settings: Settings | None = None) -> str:
    return OutboundCallService(settings)._exotel_inbound_flow_url()


outbound_call_service = OutboundCallService()
