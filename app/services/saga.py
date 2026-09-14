"""Execution saga: жизненный цикл торгового намерения.

Раздел 6 ТЗ:
    PLANNED -> RESERVED -> SUBMITTED -> CONFIRMED/FAILED/UNKNOWN -> RECONCILED
    без blind retry.

Что здесь принципиально:

* Каждый внешний write-вызов проходит через Intent с ключом
  идемпотентности. Повтор с тем же ключом не создаёт вторую покупку.
* Таймаут ПОСЛЕ отправки запроса даёт UNKNOWN, а не FAILED. Из UNKNOWN
  единственный выход — сверка с площадкой (reconcile).
* Переходы валидируются по таблице INTENT_TRANSITIONS: «перепрыгнуть»
  состояние нельзя.
"""

from __future__ import annotations

import hashlib
import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.enums import (
    INTENT_TRANSITIONS,
    Currency,
    IntentKind,
    IntentStatus,
    Market,
    TradeMode,
)
from app.models import AuditLog, Intent, utcnow

log = logging.getLogger(__name__)


class SagaError(Exception):
    """Ошибка жизненного цикла намерения."""


class InvalidTransition(SagaError):
    """Недопустимый переход состояния."""


def make_idempotency_key(
    *, kind: IntentKind, market: Market, external_id: str, price: Decimal
) -> str:
    """Построить стабильный ключ идемпотентности.

    Один и тот же лот по одной и той же цене в рамках одной операции
    даёт один и тот же ключ — повторная попытка не купит второй раз.
    """
    raw = f"{kind.value}:{market.value}:{external_id}:{price}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"{kind.value}-{market.value}-{digest}"


def find_by_key(session: Session, idempotency_key: str) -> Intent | None:
    """Найти намерение по ключу идемпотентности."""
    return (
        session.query(Intent).filter_by(idempotency_key=idempotency_key).one_or_none()
    )


def plan(
    session: Session,
    *,
    kind: IntentKind,
    market: Market,
    mode: TradeMode,
    external_id: str | None,
    price: Decimal,
    currency: Currency = Currency.STARS,
    strategy_id: int | None = None,
    gift_id: int | None = None,
    decision: dict | None = None,
) -> Intent:
    """Создать намерение в статусе PLANNED.

    Если намерение с таким ключом уже есть — возвращается существующее.
    Это и есть защита от дублирующей покупки.
    """
    key = make_idempotency_key(
        kind=kind, market=market, external_id=external_id or "", price=price
    )
    existing = find_by_key(session, key)
    if existing is not None:
        log.info(
            "Намерение с ключом %s уже существует (id=%s, статус=%s)",
            key,
            existing.id,
            existing.status,
        )
        return existing

    intent = Intent(
        idempotency_key=key,
        kind=kind,
        status=IntentStatus.PLANNED,
        market=market,
        mode=mode,
        strategy_id=strategy_id,
        gift_id=gift_id,
        listing_external_id=external_id,
        planned_price=price,
        currency=currency,
        decision=decision or {},
    )
    session.add(intent)
    session.flush()
    _audit(session, "intent.plan", intent, ok=True)
    return intent


def transition(
    session: Session,
    intent: Intent,
    new_status: IntentStatus,
    *,
    error: str | None = None,
    external_ref: str | None = None,
    executed_price: Decimal | None = None,
) -> Intent:
    """Перевести намерение в новое состояние с проверкой допустимости.

    Raises:
        InvalidTransition: переход запрещён таблицей переходов.
    """
    current = intent.status
    allowed = INTENT_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise InvalidTransition(
            f"Переход {current.value} -> {new_status.value} запрещён "
            f"(допустимо: {sorted(s.value for s in allowed) or 'ничего'})"
        )

    intent.status = new_status
    if error:
        intent.error = error[:2000]
    if external_ref:
        intent.external_ref = external_ref
    if executed_price is not None:
        intent.executed_price = executed_price

    if new_status is IntentStatus.SUBMITTED:
        intent.submitted_at = utcnow()
    if new_status in {
        IntentStatus.CONFIRMED,
        IntentStatus.FAILED,
        IntentStatus.RECONCILED,
        IntentStatus.CANCELLED,
        IntentStatus.EXPIRED,
    }:
        intent.settled_at = utcnow()

    session.flush()
    log.info(
        "Намерение #%s: %s -> %s%s",
        intent.id,
        current.value,
        new_status.value,
        f" ({error})" if error else "",
    )
    _audit(
        session,
        f"intent.{new_status.value}",
        intent,
        ok=new_status
        not in {IntentStatus.FAILED, IntentStatus.UNKNOWN, IntentStatus.EXPIRED},
    )
    return intent


def mark_unknown(session: Session, intent: Intent, detail: str) -> Intent:
    """Пометить исход неизвестным.

    Слепой повтор из этого состояния запрещён: единственный допустимый
    следующий шаг — сверка с площадкой.
    """
    return transition(session, intent, IntentStatus.UNKNOWN, error=detail)


def pending_reconciliation(session: Session, limit: int = 50) -> list[Intent]:
    """Намерения, требующие сверки: UNKNOWN и подтверждённые без сверки."""
    return (
        session.query(Intent)
        .filter(
            Intent.status.in_(
                [
                    IntentStatus.UNKNOWN.value,
                    IntentStatus.CONFIRMED.value,
                    IntentStatus.FAILED.value,
                ]
            )
        )
        .order_by(Intent.updated_at.asc())
        .limit(limit)
        .all()
    )


def _audit(session: Session, action: str, intent: Intent, *, ok: bool) -> None:
    """Записать событие саги в журнал аудита."""
    session.add(
        AuditLog(
            actor="saga",
            action=action,
            target=f"intent:{intent.id}",
            ok=ok,
            payload={
                "kind": str(intent.kind),
                "market": str(intent.market),
                "external_id": intent.listing_external_id,
                "planned_price": str(intent.planned_price or ""),
                "executed_price": str(intent.executed_price or ""),
                "error": (intent.error or "")[:300],
            },
        )
    )
