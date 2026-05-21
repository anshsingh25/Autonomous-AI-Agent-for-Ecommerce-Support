"""Orchestrate Shopify order-confirmation outbound calls and tag updates."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.database.factory import create_order_repository
from app.database.shopify_store import ShopifyRepository
from app.models.call_outcome import CallOutcome
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import get_context, save_context
from app.services.shipping_snapshot_cache import cache_from_shopify_raw
from app.services.confirmation_intent import detect_confirmation_intent
from app.services.outbound_call_service import OutboundCallService, outbound_call_service
from app.services.shopify_tags import (
    TAG_CALL_IN_PROGRESS,
    TAGS_CANCELLED,
    TAGS_CONFIRMED,
    TAGS_NO_ANSWER,
)
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)

_finalize_locks: dict[str, asyncio.Lock] = {}


class OrderConfirmationService:
    def __init__(
        self,
        settings: Settings | None = None,
        outbound: OutboundCallService | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._outbound = outbound or outbound_call_service
        self._shopify: ShopifyRepository | None = None
        if self._settings.data_source.lower() == "shopify":
            repo = create_order_repository(self._settings)
            if isinstance(repo, ShopifyRepository):
                self._shopify = repo

    def _require_shopify(self) -> ShopifyRepository:
        if not self._shopify:
            raise ValueError("Shopify confirmation requires DATA_SOURCE=shopify")
        return self._shopify

    async def process_shopify_order_webhook(
        self,
        order_payload: dict[str, Any],
        *,
        allow_when_auto_disabled: bool = False,
    ) -> dict[str, Any]:
        """Handle Shopify orders/create or orders/paid webhook (or manual dial)."""
        if not self._settings.auto_confirmation_enabled and not allow_when_auto_disabled:
            return {"status": "skipped", "reason": "AUTO_CONFIRMATION_ENABLED is false"}

        shopify = self._require_shopify()
        order_id_display = str(order_payload.get("name") or "").lstrip("#")
        shopify_id = order_payload.get("id")

        raw = order_payload
        if shopify_id and not order_payload.get("fulfillment_status"):
            fetched = await shopify.get_raw_order_by_shopify_id(shopify_id)
            if fetched:
                raw = fetched
                # Keep API overrides (phone often missing from Shopify on Basic plan).
                if order_payload.get("phone"):
                    raw["phone"] = order_payload["phone"]

        eligible, reason = shopify.is_eligible_for_confirmation_call(raw)
        if not eligible:
            logger.info("Skip confirmation call order=%s: %s", order_id_display, reason)
            return {"status": "skipped", "order_id": order_id_display, "reason": reason}

        cache_from_shopify_raw(order_id_display or str(shopify_id), raw)

        phone = shopify.extract_phone(raw)
        if not phone:
            return {
                "status": "skipped",
                "order_id": order_id_display,
                "reason": shopify.phone_missing_reason(raw),
            }

        try:
            dial_result = await self._outbound.dial_confirmation_call(
                order_id_display or str(shopify_id),
                phone=phone,
            )
        except Exception as exc:
            logger.exception("Failed to dial confirmation for order %s", order_id_display)
            return {"status": "error", "order_id": order_id_display, "message": str(exc)}

        session_id = dial_result.get("session_id")
        if session_id:
            ctx = get_context(session_id)
            if ctx:
                ctx.shopify_order_gid = str(shopify_id) if shopify_id else None
                ctx.fulfillment_status = raw.get("fulfillment_status")
                save_context(ctx)
            await shopify.append_tags(order_id_display or str(shopify_id), [TAG_CALL_IN_PROGRESS])

        return {"status": "dialing", "order_id": order_id_display, **dial_result}

    async def finalize_call(self, session_id: str, *, force_no_answer: bool = False) -> dict[str, Any]:
        """Apply Shopify tags and actions after the voice call ends."""
        lock = _finalize_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            ctx = get_context(session_id)
            if not ctx:
                return {"status": "skipped", "reason": "session not found"}
            if ctx.outcome_applied:
                return {
                    "status": "skipped",
                    "reason": "already finalized",
                    "outcome": ctx.outcome.value,
                    "order_id": ctx.order_id,
                    "session_id": session_id,
                }

            shopify = self._shopify
            transcript = " ".join(ctx.user_transcripts).strip()

            if force_no_answer:
                outcome = CallOutcome.NO_ANSWER
            elif not ctx.customer_answered and not transcript:
                outcome = CallOutcome.NO_ANSWER
            else:
                outcome = detect_confirmation_intent(transcript)
                if outcome == CallOutcome.UNCLEAR and ctx.customer_answered:
                    outcome = CallOutcome.NO_ANSWER

            ctx.outcome = outcome
            result: dict[str, Any] = {
                "status": "finalized",
                "session_id": session_id,
                "order_id": ctx.order_id,
                "outcome": outcome.value,
                "transcript_excerpt": transcript[:500],
            }

            if shopify:
                tag_result = await self._apply_shopify_outcome(ctx, outcome)
                result["shopify"] = tag_result
            else:
                result["shopify"] = {"skipped": True, "reason": "not shopify data source"}

            ctx.outcome_applied = True
            save_context(ctx)
            logger.info(
                "Confirmation finalized order=%s outcome=%s",
                ctx.order_id,
                outcome.value,
            )
            return result

    async def _apply_shopify_outcome(
        self, ctx: OutboundCallContext, outcome: CallOutcome
    ) -> dict[str, Any]:
        shopify = self._require_shopify()
        order_id = ctx.order_id

        if outcome == CallOutcome.CONFIRMED:
            tag_res = await shopify.reconcile_tags(
                order_id,
                add_tags=list(TAGS_CONFIRMED),
                remove_tags=[*TAGS_NO_ANSWER, *TAGS_CANCELLED, TAG_CALL_IN_PROGRESS],
            )
            return {
                "action": "confirmed",
                "tags": tag_res,
                "message": "Order marked confirmed; cleared Retry/Cancelled/In-Progress tags",
            }

        if outcome == CallOutcome.CANCELLED:
            cancel_res = await shopify.cancel_order_api(
                order_id,
                reason="Customer cancelled during AI confirmation call",
            )
            tag_res = await shopify.reconcile_tags(
                order_id,
                add_tags=list(TAGS_CANCELLED),
                remove_tags=[*TAGS_CONFIRMED, *TAGS_NO_ANSWER, TAG_CALL_IN_PROGRESS],
            )
            return {
                "action": "cancelled",
                "cancel": cancel_res,
                "tags": tag_res,
            }

        tag_res = await shopify.reconcile_tags(
            order_id,
            add_tags=list(TAGS_NO_ANSWER),
            remove_tags=[TAG_CALL_IN_PROGRESS],
        )
        return {
            "action": "no_answer",
            "tags": tag_res,
            "message": "Tagged Retry Call for follow-up",
        }

    async def list_pending_confirmation_orders(
        self, *, limit: int = 50
    ) -> dict[str, Any]:
        """Shopify unfulfilled orders missing Proceed / AI Confirmed (need outbound call)."""
        shopify = self._require_shopify()
        orders = await shopify.list_recent_unfulfilled_orders(limit=limit)
        pending: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw in orders:
            name = str(raw.get("name", "")).lstrip("#")
            tags = shopify.parse_tags(raw.get("tags"))
            needs, reason = shopify.order_needs_confirmation(raw)
            phone = shopify.extract_phone(raw) or None
            can_dial, dial_reason = shopify.is_eligible_for_confirmation_call(raw)
            row = {
                "order_id": name,
                "tags": tags,
                "phone": phone,
                "total_inr": float(raw.get("total_price") or 0),
                "financial_status": raw.get("financial_status"),
                "needs_call": needs,
                "can_dial": can_dial,
                "reason": reason if not needs else (dial_reason if not can_dial else "ready"),
            }
            if needs:
                pending.append(row)
            else:
                skipped.append(row)
        return {
            "pending_count": len(pending),
            "skipped_count": len(skipped),
            "pending": pending,
            "skipped": skipped,
        }

    async def dial_order_confirmation(
        self,
        order_id: str,
        *,
        phone: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Load order from Shopify, verify tags (not confirmed), then place outbound call.
        """
        shopify = self._require_shopify()
        raw = await shopify.get_raw_order(order_id)
        if not raw:
            raise ValueError(f"Order {order_id} not found in Shopify")

        if phone:
            raw = {**raw, "phone": phone.strip()}

        if not force:
            eligible, reason = shopify.is_eligible_for_confirmation_call(raw)
            if not eligible:
                return {
                    "status": "skipped",
                    "order_id": str(raw.get("name", order_id)).lstrip("#"),
                    "reason": reason,
                    "tags": shopify.parse_tags(raw.get("tags")),
                }
        elif not shopify.extract_phone(raw):
            return {
                "status": "skipped",
                "order_id": str(raw.get("name", order_id)).lstrip("#"),
                "reason": shopify.phone_missing_reason(raw),
            }

        return await self.process_shopify_order_webhook(
            raw, allow_when_auto_disabled=True
        )

    async def call_all_unconfirmed(
        self,
        *,
        limit: int = 50,
        delay_sec: float = 2.5,
    ) -> dict[str, Any]:
        """Dial every unconfirmed order that has a phone; report others needing manual entry."""
        shopify = self._require_shopify()
        orders = await shopify.list_recent_unfulfilled_orders(limit=limit)
        dialed: list[dict[str, Any]] = []
        need_phone: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for raw in orders:
            name = str(raw.get("name", "")).lstrip("#")
            needs, reason = shopify.order_needs_confirmation(raw)
            if not needs:
                skipped.append({"order_id": name, "reason": reason})
                continue
            phone = shopify.extract_phone(raw)
            if not phone:
                need_phone.append(
                    {
                        "order_id": name,
                        "tags": shopify.parse_tags(raw.get("tags")),
                        "reason": shopify.phone_missing_reason(raw),
                    }
                )
                continue
            try:
                result = await self.dial_order_confirmation(name, phone=phone)
                dialed.append(result)
            except Exception as exc:
                dialed.append(
                    {"order_id": name, "status": "error", "message": str(exc)}
                )
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)

        return {
            "dialed_count": len([d for d in dialed if d.get("status") == "dialing"]),
            "need_phone_count": len(need_phone),
            "skipped_count": len(skipped),
            "dialed": dialed,
            "need_phone": need_phone,
            "skipped": skipped,
        }

    async def scan_and_call_unfulfilled(
        self,
        *,
        limit: int = 20,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Manual/cron: find unfulfilled orders without confirmation tags and dial."""
        shopify = self._require_shopify()
        orders = await shopify.list_recent_unfulfilled_orders(limit=limit)
        results: list[dict[str, Any]] = []
        to_dial = 0
        for raw in orders:
            eligible, reason = shopify.is_eligible_for_confirmation_call(raw)
            name = str(raw.get("name", "")).lstrip("#")
            if not eligible:
                results.append({"order_id": name, "status": "skipped", "reason": reason})
                continue
            to_dial += 1
            if dry_run:
                results.append(
                    {
                        "order_id": name,
                        "status": "would_dial",
                        "phone": shopify.extract_phone(raw),
                        "tags": shopify.parse_tags(raw.get("tags")),
                    }
                )
                continue
            try:
                r = await self.process_shopify_order_webhook(raw)
                results.append(r)
            except Exception as exc:
                results.append({"order_id": name, "status": "error", "message": str(exc)})
        return {
            "dry_run": dry_run,
            "scanned": len(orders),
            "would_dial_or_dialed": to_dial,
            "processed": len(results),
            "results": results,
        }


order_confirmation_service = OrderConfirmationService()
