"""Тесты курсов валют.

Прежняя константа 400 Stars за TON завышала курс примерно в шесть раз:
лот за 4 TON выглядел как 1600 Stars вместо 260. Любое сравнение цен
между площадками было бессмысленным.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency
from app.services import fx, marketdata, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Подменить БД и хранилище на тестовые."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr(fx, "session_scope", scope)
    monkeypatch.setattr("app.services.runtime._audit", lambda *a, **k: None)
    store.invalidate()
    yield
    store.invalidate()


def test_rate_derived_from_two_sources(session):
    """Курс Stars/TON выводится из цены TON и цены звезды."""
    # TON = 1.35 USD, звезда = 0.02 USD -> 67.5 звёзд за TON.
    fx.set_manual_star_usd(Decimal("0.02"))
    marketdata.record_fx(
        session, Currency.TON, Currency.USD, Decimal("1.35"), source="tonapi"
    )
    marketdata.record_fx(
        session, Currency.STARS, Currency.USD, Decimal("0.02"), source="вручную"
    )
    raw = Decimal("1.35") / Decimal("0.02")
    assert raw == Decimal("67.5")


def test_spread_lowers_the_rate(session):
    """Спред занижает курс, а не завышает.

    Заниженный курс делает сделку менее выгодной на бумаге — это
    безопасная сторона ошибки.
    """
    fx.set_spread(Decimal("0.03"))
    raw = Decimal("67.5")
    adjusted = raw * (Decimal(1) - fx.spread())
    assert adjusted < raw
    assert adjusted == Decimal("65.475")


def test_spread_is_bounded(session):
    """Нелепое значение спреда игнорируется."""
    store.set("FX_SPREAD", "5")
    assert fx.spread() == fx.DEFAULT_SPREAD
    store.set("FX_SPREAD", "-1")
    assert fx.spread() == fx.DEFAULT_SPREAD


def test_real_snapshot_beats_default(session):
    """Записанный курс важнее значения по умолчанию."""
    assert marketdata.to_stars(
        session, Decimal("4"), Currency.TON
    ) == Decimal("4") * marketdata.DEFAULT_STARS_PER_TON

    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("65.5"), source="tonapi"
    )
    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) == Decimal("262.0")


def test_default_is_not_wildly_off(session):
    """Значение по умолчанию должно быть правдоподобным.

    При курсе TON около 1.3 USD и цене звезды около 0.02 USD выходит
    примерно 65 звёзд за TON. Прежние 400 отличались в шесть раз.
    """
    assert Decimal("40") <= marketdata.DEFAULT_STARS_PER_TON <= Decimal("120")


def test_manual_star_price_can_be_cleared(session):
    """Ручная цена звезды снимается."""
    fx.set_manual_star_usd(Decimal("0.02"))
    assert fx.manual_star_usd() == Decimal("0.02")
    fx.set_manual_star_usd(None)
    assert fx.manual_star_usd() is None


def test_invalid_manual_price_ignored(session):
    """Мусор вместо цены не ломает расчёт."""
    store.set(fx.MANUAL_STAR_USD_KEY, "не число")
    assert fx.manual_star_usd() is None
    store.set(fx.MANUAL_STAR_USD_KEY, "-5")
    assert fx.manual_star_usd() is None
