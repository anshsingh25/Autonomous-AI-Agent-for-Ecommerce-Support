"""Orchestrate Magento order-confirmation outbound calls (separate from Shopify)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.database.factory import create_order_repository
from app.database.magento_store import MagentoRepository
from app.models.call_outcome import CallOutcome
from app.models.call_context import OutboundCallContext
from app.services.call_context_store import get_context, save_context
from app.services.confirmation_intent import detect_confirmation_intent
from app.services.order_desk_store import get_order_desk_store, order_desk_primary
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
    inc = str(order_payload.get("increment_id") or "").strip()
    if inc:
        return inc.upper()
    on = order_payload.get("order_number")
    if on is not None and str(on).strip():
        return str(on).strip().upper()
    return ""


class MagentoOrderConfirmationService:
    def __init__(
        self,
        settings: Settings | None = None,
        outbound: OutboundCallService | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._outbound = outbound or outbound_call_service
        self._magento: MagentoRepository | None = None
        if self._settings.data_source.lower() == "magento":
            repo = create_order_repository(self._settings)
            if isinstance(repo, MagentoRepository):
                self._magento = repo

    def _require_magento(self) -> MagentoRepository:
        if not self._magento:
            raise ValueError("Magento confirmation requires DATA_SOURCE=magento")
        return self._magento

    async def process_magento_order_webhook(
        self,
        order_payload: dict[str, Any],
        *,
        allow_when_auto_disabled: bool = False,
        skip_eligibility: bool = False,
    ) -> dict[str, Any]:
        if not self._settings.auto_confirmation_enabled and not allow_when_auto_disabled:
            return {"status": "skipped", "reason": "AUTO_CONFIRMATION_ENABLED is false"}

        magento = self._require_magento()
        order_id_display = _webhook_order_display_id(order_payload)
        entity_id = order_payload.get("entity_id")
        store_view = str(
            order_payload.get("store_view_code")
            or order_payload.get("store_code")
            or ""
        ).strip() or None

        raw = order_payload
        if entity_id and not order_payload.get("items"):
            fetched = await magento.get_raw_order_by_entity_id(
                entity_id, store_view_code=store_view
            )
            if fetched:
                raw = fetched
                if order_payload.get("telephone"):
                    raw["telephone"] = order_payload["telephone"]
        elif order_id_display and not order_payload.get("items"):
            fetched = await magento.get_raw_order(
                order_id_display, store_view_code=store_view
            )
            if fetched:
                raw = fetched

        if not order_id_display:
            order_id_display = str(raw.get("increment_id") or entity_id or "").upper()

        if not allow_when_auto_disabled:
            trig = self._settings.confirmation_webhook_require_tag.strip()
            if trig:
                desk_tag_list = self._desk_tags(order_id_display) if order_id_display else None
                magento_markers = ", ".join(magento.parse_markers_from_histories(raw))
                if not order_has_trigger_tag(magento_markers, desk_tag_list, trig):
                    return {
                        "status": "skipped",
                        "order_id": order_id_display,
                        "reason": f"Order lacks marker {trig!r} (Magento history + order desk)",
                    }

        if not skip_eligibility:
            eligible, reason = magento.is_eligible_for_confirmation_call(raw)
            if not eligible and order_desk_primary():
                desk_rec = get_order_desk_store().get(order_id_display)
                ff = str((desk_rec or {}).get("fulfillment_status") or "").lower()
                if desk_rec and ff == "unfulfilled":
                    eligible, reason = True, ""
            if not eligible:
                return {"status": "skipped", "order_id": order_id_display, "reason": reason}

        phone = ""
        if order_desk_primary():
            desk_rec = get_order_desk_store().get(order_id_display)
            if desk_rec:
                ship = desk_rec.get("shipping") if isinstance(desk_rec.get("shipping"), dict) else {}
                phone = str(desk_rec.get("phone") or ship.get("phone") or "").strip()
        if not phone:
            phone = magento.extract_phone(raw)
        if not phone:
            return {
                "status": "skipped",
                "order_id": order_id_display,
                "reason": magento.phone_missing_reason(raw),
            }

        try:
            dial_result = await self._outbound.dial_confirmation_call(
                order_id_display,
                phone=phone,
            )
        except Exception as exc:
            return {"status": "error", "order_id": order_id_display, "message": str(exc)}

        session_id = dial_result.get("session_id")
        if session_id:
            ctx = get_context(session_id)
            if ctx and raw.get("entity_id"):
                ctx.shopify_order_id = int(raw["entity_id"])
                save_context(ctx)
            await magento.append_marker(order_id_display, TAG_CALL_IN_PROGRESS)

        return {"status": "dialing", "order_id": order_id_display, **dial_result}

    async def finalize_call(self, session_id: str, *, force_no_answer: bool = False) -> dict[str, Any]:
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

            magento = self._magento
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

            if magento:
                result["magento"] = await self._apply_magento_outcome(ctx, outcome)
            else:
                result["magento"] = {"skipped": True, "reason": "not magento data source"}

            ctx.outcome_applied = True
            save_context(ctx)
            return result

    async def _apply_magento_outcome(
        self, ctx: OutboundCallContext, outcome: CallOutcome
    ) -> dict[str, Any]:
        magento = self._require_magento()
        order_id = ctx.order_id

        if outcome == CallOutcome.CONFIRMED:
            marker_res = await magento.reconcile_markers(
                order_id,
                add_markers=list(TAGS_CONFIRMED),
            )
            return {"action": "confirmed", "markers": marker_res}

        if outcome == CallOutcome.CANCELLED:
            if ctx.shopify_cancel_via_tool:
                marker_res = await magento.reconcile_markers(
                    order_id,
                    add_markers=list(TAGS_CANCELLED),
                )
                return {"action": "cancelled", "markers": marker_res}

            excerpt = " ".join(ctx.user_transcripts).strip()[:400]
            await magento.append_order_note(
                order_id,
                f"Possible cancel intent — not auto-cancelled. Customer said: {excerpt}",
            )
            marker_res = await magento.reconcile_markers(
                order_id,
                add_markers=list(TAGS_NO_ANSWER),
            )
            return {"action": "cancel_intent_review", "markers": marker_res}

        marker_res = await magento.reconcile_markers(
            order_id,
            add_markers=list(TAGS_NO_ANSWER),
        )
        return {"action": "no_answer", "markers": marker_res}

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

    def _phone_from_order_desk(self, order_id: str) -> str | None:
        if not order_desk_primary():
            return None
        rec = get_order_desk_store().get(order_id)
        if not rec:
            return None
        ship = rec.get("shipping") if isinstance(rec.get("shipping"), dict) else {}
        p = str(rec.get("phone") or ship.get("phone") or "").strip()
        return p or None

    def _resolve_phone_from_raw_or_desk(self, raw: dict[str, Any]) -> str:
        magento = self._require_magento()
        oid = str(raw.get("increment_id", "")).strip().upper()
        phone = ""
        if order_desk_primary() and oid:
            phone = self._phone_from_order_desk(oid) or ""
        if not phone:
            phone = magento.extract_phone(raw)
        return phone

    async def dial_order_confirmation(
        self,
        order_id: str,
        *,
        phone: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        magento = self._require_magento()
        raw = await magento.get_raw_order(order_id)
        if not raw:
            raise ValueError(f"Order {order_id} not found in Magento")

        if phone:
            raw = {**raw, "telephone": phone.strip()}
        elif not magento.extract_phone(raw):
            desk_phone = self._phone_from_order_desk(order_id)
            if desk_phone:
                raw = {**raw, "telephone": desk_phone}

        if not force:
            eligible, reason = magento.is_eligible_for_confirmation_call(raw)
            if not eligible and order_desk_primary():
                desk_rec = get_order_desk_store().get(order_id)
                ff = str((desk_rec or {}).get("fulfillment_status") or "").lower()
                if desk_rec and ff == "unfulfilled":
                    eligible, reason = True, ""
            if not eligible:
                return {
                    "status": "skipped",
                    "order_id": str(raw.get("increment_id", order_id)).upper(),
                    "reason": reason,
                    "markers": magento.parse_markers_from_histories(raw),
                }
        elif not magento.extract_phone(raw):
            return {
                "status": "skipped",
                "order_id": str(raw.get("increment_id", order_id)).upper(),
                "reason": magento.phone_missing_reason(raw),
            }

        return await self.process_magento_order_webhook(
            raw,
            allow_when_auto_disabled=True,
            skip_eligibility=force,
        )

    async def list_pending_confirmation_orders(self, *, limit: int = 50) -> dict[str, Any]:
        magento = self._require_magento()
        orders = await magento.list_recent_unfulfilled_orders(limit=limit)
        pending: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for raw in orders:
            name = str(raw.get("increment_id", "")).upper()
            desk_tags = self._desk_tags(name)
            markers_csv = ", ".join(magento.parse_markers_from_histories(raw))
            tags = merged_visible_tags(markers_csv, desk_tags)
            needs, reason = magento.order_needs_confirmation(raw)
            phone = self._resolve_phone_from_raw_or_desk(raw) or None
            row = {
                "order_id": name,
                "tags": tags,
                "phone": phone,
                "total_inr": float(raw.get("grand_total") or 0),
                "status": raw.get("status"),
                "needs_call": needs,
                "can_dial": bool(needs and phone),
                "reason": reason,
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

    async def scan_and_call_unfulfilled(
        self,
        *,
        limit: int = 20,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        magento = self._require_magento()
        orders = await magento.list_recent_unfulfilled_orders(limit=limit)
        results: list[dict[str, Any]] = []
        to_dial = 0
        for raw in orders:
            name = str(raw.get("increment_id", "")).upper()
            needs, skip_reason = magento.order_needs_confirmation(raw)
            phone = self._resolve_phone_from_raw_or_desk(raw)
            if not needs:
                results.append({"order_id": name, "status": "skipped", "reason": skip_reason})
                continue
            if not phone:
                results.append(
                    {
                        "order_id": name,
                        "status": "skipped",
                        "reason": magento.phone_missing_reason(raw),
                    }
                )
                continue
            to_dial += 1
            if dry_run:
                results.append({"order_id": name, "status": "would_dial", "phone": phone})
                continue
            try:
                results.append(await self.process_magento_order_webhook(raw))
            except Exception as exc:
                results.append({"order_id": name, "status": "error", "message": str(exc)})
        return {
            "dry_run": dry_run,
            "scanned": len(orders),
            "would_dial_or_dialed": to_dial,
            "processed": len(results),
            "results": results,
        }


magento_order_confirmation_service = MagentoOrderConfirmationService()
