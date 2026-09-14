"""Атомарный бюджетный ledger.

Раздел 6 ТЗ: резерв ДО внешнего вызова, общий hard cap, суточные
лимиты, release/expire/reconcile.

Главный инвариант:

    reserved + spent <= hard_cap

Он проверяется под блокировкой строки бюджета в той же транзакции,
в которой создаётся резерв. Это и есть защита от overspend при
параллельно работающих стратегиях — из раздела 9 «Главные риски».
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import is_postgres
from app.enums import Currency, ReservationStatus
from app.models import Budget, Reservation, utcnow

log = logging.getLogger(__name__)


class BudgetError(Exception):
    """Базовая ошибка бюджета."""


class InsufficientBudget(BudgetError):
    """Не хватает свободных средств под резерв."""


class DailyLimitReached(BudgetError):
    """Исчерпан суточный лимит расходов."""


def _lock_budget(session: Session, budget_id: int) -> Budget:
    """Взять бюджет под блокировку строки.

    В PostgreSQL это SELECT ... FOR UPDATE; в SQLite сериализация
    обеспечивается самой блокировкой записи в БД.
    """
    stmt = select(Budget).where(Budget.id == budget_id)
    if is_postgres():
        stmt = stmt.with_for_update()
    budget = session.execute(stmt).scalar_one_or_none()
    if budget is None:
        raise BudgetError(f"Бюджет id={budget_id} не найден")
    return budget


def _roll_daily(budget: Budget) -> None:
    """Сбросить суточный счётчик, если начались новые сутки UTC."""
    now = utcnow()
    if budget.daily_reset_at is None or budget.daily_reset_at.date() < now.date():
        budget.daily_spent = Decimal("0")
        budget.daily_reset_at = now


def get_or_create_budget(
    session: Session,
    name: str = "main",
    *,
    hard_cap: Decimal | None = None,
    daily_limit: Decimal | None = None,
) -> Budget:
    """Найти бюджет по имени или создать с лимитами из конфига."""
    budget = session.query(Budget).filter_by(name=name).one_or_none()
    if budget is None:
        budget = Budget(
            name=name,
            currency=Currency.STARS,
            hard_cap=hard_cap if hard_cap is not None else Decimal("0"),
            daily_limit=(
                daily_limit
                if daily_limit is not None
                else Decimal(settings.daily_limit_stars)
            ),
            daily_reset_at=utcnow(),
        )
        session.add(budget)
        session.flush()
        log.info("Создан бюджет %r с потолком %s", name, budget.hard_cap)
    return budget


def reserve(
    session: Session,
    *,
    budget_id: int,
    amount: Decimal,
    intent_id: int | None = None,
    ttl_sec: int | None = None,
    note: str | None = None,
) -> Reservation:
    """Атомарно зарезервировать средства под намерение.

    Вызывается строго ДО внешнего write-вызова.

    Raises:
        InsufficientBudget: не хватает свободных средств.
        DailyLimitReached: исчерпан суточный лимит.
        BudgetError: бюджет выключен или сумма некорректна.
    """
    if amount <= 0:
        raise BudgetError("Сумма резерва должна быть положительной")

    budget = _lock_budget(session, budget_id)
    if not budget.is_active:
        raise BudgetError(f"Бюджет {budget.name!r} отключён")

    _roll_daily(budget)

    # Жёсткий потолок на одну сделку — предохранитель от ошибки в стратегии.
    max_trade = Decimal(settings.max_trade_stars or 0)
    if max_trade > 0 and amount > max_trade:
        raise BudgetError(
            f"Сумма {amount} превышает лимит на одну сделку {max_trade}"
        )

    if amount > budget.available:
        raise InsufficientBudget(
            f"Бюджет {budget.name!r}: свободно {budget.available}, требуется {amount}"
        )
    if amount > budget.daily_available:
        raise DailyLimitReached(
            f"Бюджет {budget.name!r}: суточный остаток {budget.daily_available}, "
            f"требуется {amount}"
        )

    budget.reserved = Decimal(budget.reserved) + amount

    reservation = Reservation(
        budget_id=budget.id,
        intent_id=intent_id,
        amount=amount,
        currency=budget.currency,
        status=ReservationStatus.ACTIVE,
        expires_at=utcnow()
        + dt.timedelta(seconds=ttl_sec or settings.reservation_ttl_sec),
        note=note,
    )
    session.add(reservation)
    session.flush()
    log.info(
        "Резерв #%s: %s из бюджета %r (свободно осталось %s)",
        reservation.id,
        amount,
        budget.name,
        budget.available,
    )
    return reservation


def release(session: Session, reservation_id: int, *, reason: str = "") -> None:
    """Освободить резерв: сделка не состоялась."""
    reservation = session.get(Reservation, reservation_id)
    if reservation is None or reservation.status is not ReservationStatus.ACTIVE:
        return
    budget = _lock_budget(session, reservation.budget_id)
    budget.reserved = max(
        Decimal("0"), Decimal(budget.reserved) - Decimal(reservation.amount)
    )
    reservation.status = ReservationStatus.RELEASED
    reservation.note = (reservation.note or "") + f" | освобождён: {reason}"
    log.info("Резерв #%s освобождён: %s", reservation_id, reason)


def settle(
    session: Session, reservation_id: int, *, actual_amount: Decimal | None = None
) -> None:
    """Списать резерв по факту состоявшейся сделки.

    Фактическая сумма может быть меньше зарезервированной — разница
    возвращается в свободный остаток.
    """
    reservation = session.get(Reservation, reservation_id)
    if reservation is None or reservation.status is not ReservationStatus.ACTIVE:
        return
    spent = (
        Decimal(actual_amount) if actual_amount is not None else Decimal(reservation.amount)
    )
    budget = _lock_budget(session, reservation.budget_id)
    _roll_daily(budget)

    budget.reserved = max(
        Decimal("0"), Decimal(budget.reserved) - Decimal(reservation.amount)
    )
    budget.spent = Decimal(budget.spent) + spent
    budget.daily_spent = Decimal(budget.daily_spent) + spent

    reservation.status = ReservationStatus.SETTLED
    reservation.settled_amount = spent
    log.info("Резерв #%s списан на %s", reservation_id, spent)


def credit(session: Session, budget_id: int, amount: Decimal, *, note: str = "") -> None:
    """Вернуть выручку от продажи обратно в оборотный бюджет."""
    if amount <= 0:
        return
    budget = _lock_budget(session, budget_id)
    # Выручка уменьшает накопленный расход — оборотные средства растут.
    budget.spent = Decimal(budget.spent) - amount
    log.info("Бюджет %r пополнен на %s (%s)", budget.name, amount, note)


def expire_stale(session: Session) -> int:
    """Освободить протухшие резервы.

    Вызывается воркером: если внешний вызов так и не был сделан,
    деньги не должны висеть заблокированными вечно.
    """
    now = utcnow()
    stale = (
        session.query(Reservation)
        .filter(
            Reservation.status == ReservationStatus.ACTIVE,
            Reservation.expires_at < now,
        )
        .all()
    )
    for reservation in stale:
        budget = _lock_budget(session, reservation.budget_id)
        budget.reserved = max(
            Decimal("0"), Decimal(budget.reserved) - Decimal(reservation.amount)
        )
        reservation.status = ReservationStatus.EXPIRED
    if stale:
        log.info("Протухших резервов освобождено: %s", len(stale))
    return len(stale)


def snapshot(session: Session, budget_id: int) -> dict:
    """Срез состояния бюджета для UI."""
    budget = session.get(Budget, budget_id)
    if budget is None:
        return {}
    _roll_daily(budget)
    return {
        "name": budget.name,
        "currency": budget.currency.value if hasattr(budget.currency, "value") else budget.currency,
        "hard_cap": Decimal(budget.hard_cap),
        "reserved": Decimal(budget.reserved),
        "spent": Decimal(budget.spent),
        "available": budget.available,
        "daily_limit": Decimal(budget.daily_limit),
        "daily_spent": Decimal(budget.daily_spent),
        "daily_available": budget.daily_available,
        "is_active": budget.is_active,
    }
