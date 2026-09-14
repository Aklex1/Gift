"""Тесты execution saga: идемпотентность и запрет слепого повтора."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import IntentKind, IntentStatus, Market, TradeMode
from app.services import saga


def _plan(session, price=Decimal("100"), external_id="slug-1"):
    """Создать типовое намерение покупки."""
    return saga.plan(
        session,
        kind=IntentKind.BUY,
        market=Market.TELEGRAM,
        mode=TradeMode.SEMI,
        external_id=external_id,
        price=price,
    )


def test_same_listing_same_price_is_idempotent(session):
    """Повторное планирование той же покупки не создаёт второе намерение."""
    first = _plan(session)
    second = _plan(session)
    assert first.id == second.id


def test_different_price_is_a_different_intent(session):
    """Другая цена — другое намерение."""
    first = _plan(session, price=Decimal("100"))
    second = _plan(session, price=Decimal("120"))
    assert first.id != second.id


def test_valid_transition_chain(session):
    """Полный успешный путь саги проходит целиком."""
    intent = _plan(session)
    saga.transition(session, intent, IntentStatus.RESERVED)
    saga.transition(session, intent, IntentStatus.SUBMITTED)
    saga.transition(session, intent, IntentStatus.CONFIRMED)
    saga.transition(session, intent, IntentStatus.RECONCILED)
    assert intent.status is IntentStatus.RECONCILED


def test_cannot_skip_states(session):
    """Нельзя перепрыгнуть через состояние."""
    intent = _plan(session)
    with pytest.raises(saga.InvalidTransition):
        saga.transition(session, intent, IntentStatus.CONFIRMED)


def test_unknown_cannot_go_back_to_submitted(session):
    """Из UNKNOWN нельзя вернуться к отправке — запрет слепого повтора.

    Это ключевое требование ТЗ: таймаут после write не даёт права
    покупать второй раз.
    """
    intent = _plan(session)
    saga.transition(session, intent, IntentStatus.RESERVED)
    saga.transition(session, intent, IntentStatus.SUBMITTED)
    saga.mark_unknown(session, intent, "таймаут")
    assert intent.status is IntentStatus.UNKNOWN

    with pytest.raises(saga.InvalidTransition):
        saga.transition(session, intent, IntentStatus.SUBMITTED)

    # Единственный допустимый выход — сверка.
    saga.transition(session, intent, IntentStatus.RECONCILED)
    assert intent.status is IntentStatus.RECONCILED


def test_terminal_state_is_final(session):
    """Из терминального состояния переходов нет."""
    intent = _plan(session)
    saga.transition(session, intent, IntentStatus.CANCELLED)
    with pytest.raises(saga.InvalidTransition):
        saga.transition(session, intent, IntentStatus.RESERVED)
