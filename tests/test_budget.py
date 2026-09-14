"""Тесты бюджетного ledger — защиты от перерасхода."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import ReservationStatus
from app.models import Budget
from app.services import budget as budget_service


@pytest.fixture()
def budget(session):
    """Бюджет с потолком 1000 Stars."""
    row = Budget(name="test", hard_cap=Decimal("1000"), daily_limit=Decimal("0"))
    session.add(row)
    session.flush()
    return row


def test_reserve_reduces_available(session, budget):
    """Резерв уменьшает свободный остаток."""
    budget_service.reserve(session, budget_id=budget.id, amount=Decimal("300"))
    assert budget.available == Decimal("700")
    assert budget.reserved == Decimal("300")


def test_reserve_cannot_exceed_hard_cap(session, budget):
    """Нельзя зарезервировать больше жёсткого потолка."""
    budget_service.reserve(session, budget_id=budget.id, amount=Decimal("800"))
    with pytest.raises(budget_service.InsufficientBudget):
        budget_service.reserve(session, budget_id=budget.id, amount=Decimal("300"))
    # Отклонённый резерв не изменил состояние.
    assert budget.reserved == Decimal("800")


def test_parallel_reservations_respect_cap(session, budget):
    """Сумма параллельных резервов не превышает потолок.

    Это главный сценарий из раздела «Параллельные стратегии»
    в рисках ТЗ.
    """
    for _ in range(10):
        try:
            budget_service.reserve(session, budget_id=budget.id, amount=Decimal("150"))
        except budget_service.InsufficientBudget:
            break
    assert budget.reserved <= Decimal("1000")
    assert budget.available >= Decimal("0")


def test_release_returns_funds(session, budget):
    """Освобождение резерва возвращает деньги в оборот."""
    reservation = budget_service.reserve(
        session, budget_id=budget.id, amount=Decimal("400")
    )
    budget_service.release(session, reservation.id, reason="тест")
    assert budget.reserved == Decimal("0")
    assert budget.available == Decimal("1000")
    assert reservation.status is ReservationStatus.RELEASED


def test_settle_moves_reserved_to_spent(session, budget):
    """Списание переносит сумму из резерва в потраченное."""
    reservation = budget_service.reserve(
        session, budget_id=budget.id, amount=Decimal("400")
    )
    budget_service.settle(session, reservation.id, actual_amount=Decimal("350"))
    assert budget.reserved == Decimal("0")
    assert budget.spent == Decimal("350")
    # Неиспользованные 50 вернулись в оборот.
    assert budget.available == Decimal("650")


def test_daily_limit_blocks_overspend(session):
    """Суточный лимит останавливает торговлю независимо от потолка."""
    row = Budget(name="daily", hard_cap=Decimal("10000"), daily_limit=Decimal("500"))
    session.add(row)
    session.flush()

    reservation = budget_service.reserve(
        session, budget_id=row.id, amount=Decimal("400")
    )
    budget_service.settle(session, reservation.id)
    with pytest.raises(budget_service.DailyLimitReached):
        budget_service.reserve(session, budget_id=row.id, amount=Decimal("200"))


def test_expire_stale_frees_reservation(session, budget):
    """Протухший резерв освобождается автоматически."""
    import datetime as dt

    reservation = budget_service.reserve(
        session, budget_id=budget.id, amount=Decimal("200")
    )
    reservation.expires_at = dt.datetime.utcnow() - dt.timedelta(minutes=5)
    session.flush()

    freed = budget_service.expire_stale(session)
    assert freed == 1
    assert budget.reserved == Decimal("0")
