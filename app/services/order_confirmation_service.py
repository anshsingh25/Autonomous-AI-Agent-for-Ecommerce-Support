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
from app.services.order_desk_store import get_order_desk_store, order_desk_primary
from app.services.shipping_snapshot_cache import cache_from_shopify_raw
from app.services.confirmation_intent import detect_confirmation_intent
from app.services.outbound_call_service import OutboundCallService, outbound_call_service
from app.services.shopify_tags import (
    TAG_CALL_IN_PROGRESS,
    TAGS_CANCELLED,
    TAGS_CONFIRMED,
    TAGS_NO_ANSWER,
    merged_visible_tags,
    order_has_trigger_tag,
)
from app.utils.config import Settings, get_settings

logger = logging.getLogger(__name__)

_finalize_locks: dict[str, asyncio.Lock] = {}


def _webhook_order_display_id(order_payload: dict[str, Any]) -> str:
    """Shopify order name (#1601) or order_number when name is missing."""
    name = str(order_payload.get("name") or "").lstrip("#").strip()
    if name:
        return name
    on = order_payload.get("order_number")
    if on is not None and str(on).strip():
        return str(on).strip()
    return ""


def _shopify_webhook_id_invalid(shopify_id: Any) -> bool:
    """True for Shopify sample ids (exampleCartId) or non-numeric REST order ids."""
    if shopify_id is None:
        return True
    s = str(shopify_id).strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith("example") or "cart" in low:
        return True
    return not s.isdigit()


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
        skip_eligibility: bool = False,
    ) -> dict[str, Any]:
        """Handle Shopify orders/create or orders/paid webhook (or manual dial)."""
        if not self._settings.auto_confirmation_enabled and not allow_when_auto_disabled:
            return {"status": "skipped", "reason": "AUTO_CONFIRMATION_ENABLED is false"}

        shopify = self._require_shopify()
        order_id_display = _webhook_order_display_id(order_payload)
        shopify_id = order_payload.get("id")

        if _shopify_webhook_id_invalid(shopify_id) and not order_id_display:
            logger.info(
                "Skip confirmation: Shopify test/sample webhook payload (id=%r), not a real order",
                shopify_id,
            )
            return {
                "status": "skipped",
                "order_id": "",
                "reason": (
                    "Shopify test notification (exampleCartId) — not a real order. "
                    "Use a real checkout or manual Call customer on the confirmation desk."
                ),
            }

        raw = order_payload
        if (
            shopify_id
            and not _shopify_webhook_id_invalid(shopify_id)
            and not order_payload.get("fulfillment_status")
        ):
            fetched = await shopify.get_raw_order_by_shopify_id(shopify_id)
            if fetched:
                raw = fetched
                # Keep API overrides (phone often missing from Shopify on Basic plan).
                if order_payload.get("phone"):
                    raw["phone"] = order_payload["phone"]

        if not allow_when_auto_disabled:
            trig = self._settings.confirmation_webhook_require_tag.strip()
            if trig:
                oid_key = order_id_display or str(shopify_id or "")
                desk_tag_list = self._desk_tags(oid_key) if oid_key else None
                if not order_has_trigger_tag(raw.get("tags"), desk_tag_list, trig):
                    logger.info(
                        "Skip confirmation (CONFIRMATION_WEBHOOK_REQUIRE_TAG=%r) order=%s",
                        trig,
                        order_id_display,
                    )
                    return {
                        "status": "skipped",
                        "order_id": order_id_display,
                        "reason": f"Order lacks tag {trig!r} (checked Shopify tags + order desk)",
                    }

        if not skip_eligibility:
            eligible, reason = shopify.is_eligible_for_confirmation_call(raw)
            if not eligible and order_desk_primary():
                desk_rec = get_order_desk_store().get(order_id_display or str(shopify_id))
                ff = str((desk_rec or {}).get("fulfillment_status") or "").lower()
                if desk_rec and ff == "unfulfilled":
                    eligible, reason = True, ""
                    logger.info(
                        "Order desk unfulfilled overrides Shopify eligibility for %s",
                        order_id_display,
                    )
            if not eligible:
                logger.info("Skip confirmation call order=%s: %s", order_id_display, reason)
                return {"status": "skipped", "order_id": order_id_display, "reason": reason}

        cache_from_shopify_raw(order_id_display or str(shopify_id), raw)

        phone = ""
        if order_desk_primary():
            desk_rec = get_order_desk_store().get(order_id_display or str(shopify_id))
            if desk_rec:
                ship = desk_rec.get("shipping") if isinstance(desk_rec.get("shipping"), dict) else {}
                phone = str(desk_rec.get("phone") or ship.get("phone") or "").strip()
        if not phone:
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
            msg = str(exc)
            if msg.startswith("Exotel connect failed"):
                logger.warning(
                    "Exotel refused dial for order %s: %s",
                    order_id_display,
                    msg,
                )
            else:
                logger.exception(
                    "Failed to dial confirmation for order %s", order_id_display
                )
            return {"status": "error", "order_id": order_id_display, "message": msg}

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
            if ctx.shopify_cancel_via_tool:
                tag_res = await shopify.reconcile_tags(
                    order_id,
                    add_tags=list(TAGS_CANCELLED),
                    remove_tags=[*TAGS_CONFIRMED, *TAGS_NO_ANSWER, TAG_CALL_IN_PROGRESS],
                )
                return {
                    "action": "cancelled",
                    "tags": tag_res,
                    "message": "Order cancelled during call via cancel_confirmation_order tool",
                }

            # Transcript-only cancel intent — never auto-cancel in Shopify (avoids
            # false positives when customer confirms but says "cancel" in another context).
            excerpt = " ".join(ctx.user_transcripts).strip()[:400]
            note = (
                "AI call: possible cancel intent in transcript — order left open for review. "
                f'Customer said: "{excerpt}"'
            )
            await shopify.append_order_note(order_id, note)
            tag_res = await shopify.reconcile_tags(
                order_id,
                add_tags=list(TAGS_NO_ANSWER),
                remove_tags=[TAG_CALL_IN_PROGRESS],
            )
            return {
                "action": "cancel_intent_review",
                "tags": tag_res,
                "message": (
                    "Cancel keywords heard but order was NOT cancelled in Shopify — "
                    "review transcript or call customer back. Use cancel tool only after "
                    "explicit customer request."
                ),
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
            desk_tags = self._desk_tags(name)
            tags = merged_visible_tags(raw.get("tags"), desk_tags)
            needs, reason = shopify.order_needs_confirmation(raw)
            shop_phone = shopify.extract_phone(raw)
            phone = self._resolve_phone_from_raw_or_desk(raw) or None
            phone_source = ""
            if phone:
                if shop_phone:
                    phone_source = "shopify"
                elif order_desk_primary():
                    phone_source = "order_desk"
            can_dial = bool(needs and phone)
            dial_reason = "ready" if can_dial else (
                shopify.phone_missing_reason(raw) if needs else reason
            )
            row = {
                "order_id": name,
                "tags": tags,
                "phone": phone,
                "phone_source": phone_source,
                "total_inr": float(raw.get("total_price") or 0),
                "financial_status": raw.get("financial_status"),
                "needs_call": needs,
                "can_dial": can_dial,
                "reason": reason if not needs else dial_reason,
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

    @staticmethod
    def _phone_from_order_desk(order_id: str) -> str | None:
        if not order_desk_primary():
            return None
        rec = get_order_desk_store().get(order_id)
        if not rec:
            return None
        ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
        p = str(rec.get("phone") or ship.get("phone") or "").strip()
        return p or None

    def _desk_tags(self, order_id_display: str) -> list[str] | None:
        if not order_desk_primary():
            return None
        rec = get_order_desk_store().get(order_id_display)
        if not rec:
            return None
        t = rec.get("tags")
        if isinstance(t, list):
            return [str(x).strip() for x in t if str(x).strip()]
        return None

    def _resolve_phone_from_raw_or_desk(self, raw: dict[str, Any]) -> str:
        """Prefer order desk phone when Shopify omits it."""
        shopify = self._require_shopify()
        oid = str(raw.get("name", "")).lstrip("#").strip()
        phone = ""
        if order_desk_primary() and oid:
            rec = get_order_desk_store().get(oid)
            if rec:
                ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
                phone = str(rec.get("phone") or ship.get("phone") or "").strip()
        if not phone:
            phone = shopify.extract_phone(raw)
        return phone

    async def dial_order_confirmation(
        self,
        order_id: str,
        *,
        phone: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Load order from Shopify, verify tags (not confirmed), then place outbound call.
        Phone and address for the call come from order desk when Shopify omits PII.
        """
        shopify = self._require_shopify()
        raw = await shopify.get_raw_order(order_id)
        if not raw:
            raise ValueError(f"Order {order_id} not found in Shopify")

        if phone:
            raw = {**raw, "phone": phone.strip()}
        elif not shopify.extract_phone(raw):
            desk_phone = self._phone_from_order_desk(order_id)
            if desk_phone:
                raw = {**raw, "phone": desk_phone}
                logger.info(
                    "Using phone from order desk for order %s (Shopify API had none)",
                    order_id,
                )

        if not force:
            eligible, reason = shopify.is_eligible_for_confirmation_call(raw)
            if not eligible and order_desk_primary():
                desk_rec = get_order_desk_store().get(order_id)
                ff = str((desk_rec or {}).get("fulfillment_status") or "").lower()
                if desk_rec and ff == "unfulfilled":
                    eligible, reason = True, ""
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
                "reason": (
                    shopify.phone_missing_reason(raw)
                    + ' Import phone in order desk or pass "phone" in the API body.'
                ),
            }

        return await self.process_shopify_order_webhook(
            raw,
            allow_when_auto_disabled=True,
            skip_eligibility=force,
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
            phone = self._resolve_phone_from_raw_or_desk(raw)
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
            name = str(raw.get("name", "")).lstrip("#")
            needs, skip_reason = shopify.order_needs_confirmation(raw)
            phone = self._resolve_phone_from_raw_or_desk(raw)
            if not needs:
                results.append({"order_id": name, "status": "skipped", "reason": skip_reason})
                continue
            if not phone:
                results.append(
                    {
                        "order_id": name,
                        "status": "skipped",
                        "reason": shopify.phone_missing_reason(raw),
                    }
                )
                continue
            to_dial += 1
            if dry_run:
                results.append(
                    {
                        "order_id": name,
                        "status": "would_dial",
                        "phone": phone,
                        "tags": merged_visible_tags(raw.get("tags"), self._desk_tags(name)),
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

    async def list_tagged_confirmation_candidates(
        self,
        tag: str,
        *,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Unfulfilled orders that still need confirmation AND match ``tag``
        (case-insensitive), on Shopify tags and/or order desk ``tags``.

        Phone is resolved using order desk when Shopify omits PII.
        """
        shopify = self._require_shopify()
        tg = tag.strip()
        if not tg:
            raise ValueError("tag must be non-empty")

        fetch_limit = min(max(limit * 5, limit), 250)
        orders = await shopify.list_recent_unfulfilled_orders(limit=fetch_limit)
        matched: list[dict[str, Any]] = []

        for raw in orders:
            name = str(raw.get("name", "")).lstrip("#")
            needs, _reason = shopify.order_needs_confirmation(raw)
            if not needs:
                continue
            dt = self._desk_tags(name)
            if not order_has_trigger_tag(raw.get("tags"), dt, tg):
                continue
            phone = self._resolve_phone_from_raw_or_desk(raw)
            merged = merged_visible_tags(raw.get("tags"), dt)
            matched.append(
                {
                    "order_id": name,
                    "tags": merged,
                    "phone": phone or None,
                    "will_dial": bool(phone),
                    "blocking_reason": None
                    if phone
                    else shopify.phone_missing_reason(raw),
                }
            )
            if len(matched) >= limit:
                break

        return {"tag": tg, "count": len(matched), "orders": matched}

    async def dial_orders_with_matching_tag(
        self,
        tag: str,
        *,
        limit: int = 20,
        dry_run: bool = False,
        delay_sec: float = 2.5,
    ) -> dict[str, Any]:
        """
        Dial every matching tagged order that has a callable phone (desk or Shopify).

        Use from cron or automation after staff tag orders (for example ``AI Call Queue``).
        """
        preview = await self.list_tagged_confirmation_candidates(tag, limit=limit)
        results: list[dict[str, Any]] = []

        for row in preview["orders"]:
            oid = row["order_id"]
            if not row.get("phone"):
                results.append(
                    {
                        "order_id": oid,
                        "status": "need_phone",
                        "reason": row.get("blocking_reason") or "No phone",
                    }
                )
                continue
            if dry_run:
                results.append(
                    {
                        "order_id": oid,
                        "status": "would_dial",
                        "phone": row["phone"],
                        "tags": row["tags"],
                    }
                )
                continue
            try:
                r = await self.dial_order_confirmation(oid, phone=str(row["phone"]))
                results.append(r)
            except Exception as exc:
                results.append({"order_id": oid, "status": "error", "message": str(exc)})
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)

        return {
            "tag": preview["tag"],
            "dry_run": dry_run,
            "candidates": len(preview["orders"]),
            "dialed_attempts": len([r for r in results if r.get("status") == "dialing"]),
            "results": results,
        }


order_confirmation_service = OrderConfirmationService()
