"""Сквозной тест торгового цикла на фейковой площадке.

Проверяется полный путь денег: резерв -> покупка -> позиция -> продажа,
а также поведение при неизвестном исходе.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.adapters.base import (
    Capability,
    CapabilityStatus,
    ExecutionResult,
    GiftRef,
    ListingDTO,
    MarketAdapter,
    OutcomeUnknown,
)
from app.enums import (
    Confidence,
    Currency,
    IntentStatus,
    Market,
    PositionStatus,
    TradeMode,
)


class FakeTelegram(MarketAdapter):
    """Подставной адаптер вместо реального Telegram.

    Позволяет проверить торговый цикл, не тратя настоящие Stars.
    """

    market = Market.TELEGRAM
    native_currency = Currency.STARS
    capabilities = {
        cap: CapabilityStatus.SUPPORTED
        for cap in (
            Capability.SEARCH,
            Capability.BUY,
            Capability.LIST,
            Capability.REPRICE,
            Capability.CANCEL,
            Capability.INVENTORY,
            Capability.RECONCILE,
        )
    }

    def __init__(self, *, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.buy_calls = 0
        self.owned: set[str] = set()

    async def buy(self, *, external_id, expected_price, idempotency_key):
        """Сымитировать покупку с заданным исходом."""
        self.buy_calls += 1
        if self.outcome == "timeout":
            raise OutcomeUnknown("таймаут после отправки", external_ref=external_id)
        if self.outcome == "price_changed":
            return ExecutionResult(ok=False, detail="цена изменилась")
        self.owned.add(external_id)
        return ExecutionResult(
            ok=True,
            external_ref=external_id,
            executed_price=expected_price,
            currency=Currency.STARS,
        )

    async def list_for_sale(self, *, gift_ref, price, idempotency_key):
        """Сымитировать выставление."""
        return ExecutionResult(
            ok=True, external_ref=gift_ref.slug, executed_price=price
        )

    async def reconcile(self, *, external_ref, gift_ref):
        """Сымитировать сверку: подарок у нас или нет."""
        return ExecutionResult(
            ok=external_ref in self.owned,
            external_ref=external_ref,
            executed_price=Decimal("400") if external_ref in self.owned else None,
        )


@pytest.fixture()
def wired(session, monkeypatch, tmp_path):
    """Подменить БД приложения и адаптер Telegram на тестовые."""
    from sqlalchemy.orm import sessionmaker
    from contextlib import contextmanager

    from app import db as db_module
    from app.config import settings
    from app.services import executor, budget as budget_service, saga
    from app.services.valuation import seed_fee_schedules

    @contextmanager
    def fake_scope():
        """Одна и та же сессия на весь тест — видим все изменения."""
        yield session
        session.flush()

    monkeypatch.setattr("app.services.executor.session_scope", fake_scope)
    monkeypatch.setattr("app.services.reconciler.session_scope", fake_scope)

    settings.kill_switch = False
    settings.max_trade_stars = 0
    # Боевой режим площадки включается явно — в тестах тоже.
    monkeypatch.setattr(settings, "telegram_enable_write", True)
    monkeypatch.setattr("app.services.runtime.store.get", lambda key: None)
    seed_fee_schedules(session)
    return session


def _make_candidate(session, price="400"):
    """Создать стратегию с бюджетом и кандидата на покупку."""
    from app.models import Budget, Candidate, Gift, Strategy, utcnow

    budget = Budget(name="flow", hard_cap=Decimal("10000"))
    session.add(budget)
    session.flush()

    strategy = Strategy(
        name="flow-strategy",
        is_enabled=True,
        mode=TradeMode.SEMI,
        budget_id=budget.id,
        markets=[Market.TELEGRAM.value],
        min_roi=Decimal("0.1"),
        max_open_positions=5,
    )
    gift = Gift(canonical_key="testgift#1", collection="TestGift", number=1, slug="slug-1")
    session.add_all([strategy, gift])
    session.flush()

    candidate = Candidate(
        strategy_id=strategy.id,
        gift_id=gift.id,
        market=Market.TELEGRAM,
        listing_external_id="slug-1",
        price_stars=Decimal(price),
        fair_value_stars=Decimal("1000"),
        net_roi=Decimal("0.5"),
        risk_score=20,
        confidence=Confidence.HIGH,
        state="pending",
        expires_at=utcnow() + dt.timedelta(minutes=10),
    )
    session.add(candidate)
    session.flush()
    return candidate, budget, strategy


@pytest.mark.asyncio
async def test_successful_buy_creates_position(wired, monkeypatch):
    """Успешная покупка списывает резерв и открывает позицию."""
    from app.models import Intent, Position
    from app.services import executor

    session = wired
    fake = FakeTelegram(outcome="ok")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session)
    result = await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    assert result["ok"] is True, result
    assert fake.buy_calls == 1

    position = session.query(Position).one()
    assert position.buy_price == Decimal("400")
    assert position.status is PositionStatus.LOCKED

    intent = session.query(Intent).one()
    assert intent.status is IntentStatus.CONFIRMED

    # Деньги ушли из резерва в потраченное.
    assert budget.reserved == Decimal("0")
    assert budget.spent == Decimal("400")
    assert budget.available == Decimal("9600")


@pytest.mark.asyncio
async def test_price_change_releases_reservation(wired, monkeypatch):
    """Отказ площадки освобождает зарезервированные деньги."""
    from app.models import Position
    from app.services import executor

    session = wired
    fake = FakeTelegram(outcome="price_changed")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session)
    result = await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    assert result["ok"] is False
    assert session.query(Position).count() == 0
    assert budget.reserved == Decimal("0")
    assert budget.spent == Decimal("0")
    assert budget.available == Decimal("10000")


@pytest.mark.asyncio
async def test_timeout_does_not_retry_and_keeps_reservation(wired, monkeypatch):
    """Таймаут даёт UNKNOWN: повторной покупки нет, деньги держатся.

    Это защита от двойной покупки из раздела рисков ТЗ.
    """
    from app.models import Intent, Position
    from app.services import executor

    session = wired
    fake = FakeTelegram(outcome="timeout")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session)
    result = await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    assert result["ok"] is None
    assert fake.buy_calls == 1, "покупка не должна повторяться"

    intent = session.query(Intent).one()
    assert intent.status is IntentStatus.UNKNOWN
    # Резерв НЕ освобождён: деньги могли реально уйти.
    assert budget.reserved == Decimal("400")
    assert session.query(Position).count() == 0


@pytest.mark.asyncio
async def test_reconcile_resolves_unknown_outcome(wired, monkeypatch):
    """Сверка закрывает UNKNOWN и создаёт позицию, если покупка прошла."""
    from app.models import Intent, Position
    from app.services import executor, reconciler

    session = wired
    fake = FakeTelegram(outcome="timeout")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)
    monkeypatch.setattr("app.services.reconciler.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session)
    await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    # Оказалось, покупка на площадке всё-таки прошла.
    fake.owned.add("slug-1")
    await reconciler.run_once()

    intent = session.query(Intent).one()
    assert intent.status is IntentStatus.RECONCILED
    assert session.query(Position).count() == 1
    assert budget.reserved == Decimal("0")
    assert budget.spent == Decimal("400")


@pytest.mark.asyncio
async def test_reconcile_refunds_when_purchase_failed(wired, monkeypatch):
    """Если сверка показала, что покупки не было — деньги возвращаются."""
    from app.models import Intent, Position
    from app.services import executor, reconciler

    session = wired
    fake = FakeTelegram(outcome="timeout")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)
    monkeypatch.setattr("app.services.reconciler.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session)
    await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    # Подарок нам не принадлежит — покупка не прошла.
    await reconciler.run_once()

    intent = session.query(Intent).one()
    assert intent.status is IntentStatus.RECONCILED
    assert session.query(Position).count() == 0
    assert budget.reserved == Decimal("0")
    assert budget.spent == Decimal("0")
    assert budget.available == Decimal("10000")


@pytest.mark.asyncio
async def test_budget_cap_stops_buying(wired, monkeypatch):
    """Покупка дороже бюджета отклоняется до внешнего вызова."""
    from app.services import executor

    session = wired
    fake = FakeTelegram(outcome="ok")
    monkeypatch.setattr("app.services.executor.get_adapter", lambda m: fake)

    candidate, budget, _ = _make_candidate(session, price="50000")
    result = await executor.execute_buy(candidate.id, mode=TradeMode.SEMI)

    assert result["ok"] is False
    assert fake.buy_calls == 0, "внешний вызов не должен был произойти"
    assert budget.reserved == Decimal("0")
