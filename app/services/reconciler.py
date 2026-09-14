"""Сверка исходов: единственный выход из состояния UNKNOWN.

Правило из ТЗ: timeout после write нельзя повторять вслепую —
можно только пойти и посмотреть, что реально произошло на площадке.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.adapters.base import AdapterError, Capability, GiftRef
from app.adapters.registry import get_adapter
from app.db import session_scope
from app.enums import Currency, IntentKind, IntentStatus, Market, PositionStatus
from app.models import Candidate, Gift, Intent, Transaction
from app.services import budget as budget_service
from app.services import saga
from app.services.executor import _create_position

log = logging.getLogger(__name__)

#: После стольких неудачных попыток сверки намерение уходит владельцу.
MAX_ATTEMPTS = 8


async def run_once() -> dict:
    """Один проход сверки по намерениям в статусе UNKNOWN."""
    report = {"checked": 0, "resolved": 0, "escalated": 0}

    with session_scope() as session:
        pending = (
            session.query(Intent)
            .filter(Intent.status == IntentStatus.UNKNOWN.value)
            .order_by(Intent.updated_at.asc())
            .limit(20)
            .all()
        )
        plan = [
            {
                "id": i.id,
                "market": str(i.market),
                "external_ref": i.external_ref or i.listing_external_id,
                "gift_id": i.gift_id,
                "kind": str(i.kind),
                "attempts": int(i.reconcile_attempts or 0),
            }
            for i in pending
        ]

    for item in plan:
        report["checked"] += 1
        if item["attempts"] >= MAX_ATTEMPTS:
            report["escalated"] += 1
            log.error(
                "Намерение #%s не сведено за %s попыток — требуется ручная проверка",
                item["id"],
                item["attempts"],
            )
            continue

        market = Market(item["market"])
        adapter = get_adapter(market)
        if not adapter.supports(Capability.RECONCILE):
            log.warning("%s: сверка недоступна, намерение #%s зависло", market.value, item["id"])
            continue

        with session_scope() as session:
            gift = session.get(Gift, item["gift_id"]) if item["gift_id"] else None
            gift_ref = (
                GiftRef(collection=gift.collection, slug=gift.slug) if gift else None
            )

        try:
            result = await adapter.reconcile(
                external_ref=item["external_ref"], gift_ref=gift_ref
            )
        except AdapterError as exc:
            log.warning("Сверка намерения #%s не удалась: %s", item["id"], exc)
            with session_scope() as session:
                intent = session.get(Intent, item["id"])
                intent.reconcile_attempts = int(intent.reconcile_attempts or 0) + 1
            continue

        if result.ok is None:
            with session_scope() as session:
                intent = session.get(Intent, item["id"])
                intent.reconcile_attempts = int(intent.reconcile_attempts or 0) + 1
            continue

        _apply_outcome(item, result)
        report["resolved"] += 1

    if report["checked"]:
        log.info("Сверка: проверено %s, сведено %s", report["checked"], report["resolved"])
    return report


def _apply_outcome(item: dict, result) -> None:
    """Применить результат сверки к намерению, бюджету и портфелю."""
    from app.models import Reservation

    with session_scope() as session:
        intent = session.get(Intent, item["id"])
        if intent is None:
            return

        reservation = (
            session.query(Reservation).filter_by(intent_id=intent.id).first()
        )
        kind = intent.kind
        market = intent.market

        if result.ok:
            # Операция всё-таки прошла.
            executed = result.executed_price or intent.planned_price or Decimal(0)
            saga.transition(
                session,
                intent,
                IntentStatus.RECONCILED,
                external_ref=result.external_ref,
                executed_price=executed,
            )
            if reservation is not None:
                budget_service.settle(session, reservation.id, actual_amount=executed)

            if kind is IntentKind.BUY and intent.gift_id:
                exists = (
                    session.query(Intent)
                    .filter(Intent.id == intent.id, Intent.gift_id.isnot(None))
                    .first()
                )
                from app.models import Position

                already = (
                    session.query(Position)
                    .filter(
                        Position.buy_intent_id == intent.id,
                    )
                    .first()
                )
                if already is None and exists is not None:
                    position = _create_position(
                        session,
                        gift_id=intent.gift_id,
                        strategy_id=intent.strategy_id,
                        market=market,
                        price=executed,
                        intent_id=intent.id,
                    )
                    session.add(
                        Transaction(
                            intent_id=intent.id,
                            position_id=position.id,
                            market=market,
                            kind="buy",
                            amount=executed,
                            currency=Currency.STARS,
                            external_ref=result.external_ref,
                        )
                    )
            log.info("Намерение #%s сведено: операция прошла", intent.id)
        else:
            # Операция не прошла — освобождаем деньги.
            saga.transition(
                session,
                intent,
                IntentStatus.RECONCILED,
                error=f"сверка: {result.detail or 'операция не прошла'}",
            )
            if reservation is not None:
                budget_service.release(
                    session, reservation.id, reason="сверка: операция не прошла"
                )
            candidate = (
                session.query(Candidate).filter_by(intent_id=intent.id).first()
            )
            if candidate is not None:
                candidate.state = "rejected"
            log.info("Намерение #%s сведено: операция не прошла", intent.id)
