"""Тесты оценки: ROI считается после комиссий, а не до."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.enums import Confidence, Market
from app.services.marketdata import MarketSnapshot, robust_median
from app.services.valuation import (
    Fees,
    evaluate,
    net_proceeds_from,
    seed_fee_schedules,
    suggested_list_price,
)


def _snapshot(median="1000", floor=None, confidence=Confidence.HIGH, listings=10):
    """Типовой срез рынка."""
    return MarketSnapshot(
        collection="TestGift",
        model="Common",
        median_price=Decimal(median),
        floor_price=Decimal(floor) if floor else None,
        sample_size=12,
        confidence=confidence,
        velocity_per_day=1.0,
        newest=dt.datetime.utcnow(),
        active_listings=listings,
    )


def test_robust_median_rejects_outliers():
    """MAD-фильтр убирает аномальные цены (признак накрутки)."""
    values = [Decimal(x) for x in (100, 102, 98, 101, 99, 100, 5000)]
    median, cleaned = robust_median(values)
    assert Decimal(5000) not in cleaned
    assert Decimal("95") < median < Decimal("105")


def test_net_proceeds_subtracts_fees():
    """На руки приходит меньше цены продажи."""
    fees = Fees(
        sale_fee=Decimal("0.20"),
        buy_fee=Decimal("0"),
        royalty=Decimal("0"),
        network_fee=Decimal("0"),
    )
    assert net_proceeds_from(Decimal("1000"), fees) == Decimal("800")


def test_deal_profitable_before_fees_is_rejected(session):
    """Сделка с наценкой меньше комиссии признаётся убыточной.

    Купить за 850 и продать за 1000 выглядит как +17%, но при
    комиссии 20% на руки приходит 800 — это убыток.
    """
    seed_fee_schedules(session)
    result = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("850"),
        sell_market=Market.TELEGRAM,
        snapshot=_snapshot(median="1000"),
        is_official_api=True,
    )
    assert result.net_profit < 0
    assert result.blockers, "убыточная сделка должна иметь блокер"


def test_clearly_profitable_deal_passes(session):
    """Сделка с большим запасом проходит проверку."""
    seed_fee_schedules(session)
    result = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("400"),
        sell_market=Market.TELEGRAM,
        snapshot=_snapshot(median="1000"),
        is_official_api=True,
    )
    assert result.net_profit > 0
    assert result.net_roi > Decimal("0.5")
    assert not result.blockers


def test_no_market_data_blocks_trade(session):
    """Без рыночных данных торговать нельзя."""
    seed_fee_schedules(session)
    snapshot = MarketSnapshot(
        collection="Unknown",
        model=None,
        median_price=None,
        floor_price=None,
        sample_size=0,
        confidence=Confidence.NONE,
        velocity_per_day=0.0,
        newest=None,
        active_listings=0,
    )
    result = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("100"),
        sell_market=Market.TELEGRAM,
        snapshot=snapshot,
        is_official_api=True,
    )
    assert result.blockers


def test_private_api_raises_risk(session):
    """Приватный API без SLA повышает риск сделки."""
    seed_fee_schedules(session)
    common = dict(
        buy_price=Decimal("400"),
        snapshot=_snapshot(median="1000"),
    )
    official = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        sell_market=Market.TELEGRAM,
        is_official_api=True,
        **common,
    )
    private = evaluate(
        session,
        buy_market=Market.TONNEL,
        sell_market=Market.TONNEL,
        is_official_api=False,
        **common,
    )
    assert private.risk_score > official.risk_score


def test_low_confidence_raises_risk(session):
    """Слабая выборка повышает риск."""
    seed_fee_schedules(session)
    high = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("400"),
        sell_market=Market.TELEGRAM,
        snapshot=_snapshot(confidence=Confidence.HIGH),
        is_official_api=True,
    )
    low = evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("400"),
        sell_market=Market.TELEGRAM,
        snapshot=_snapshot(confidence=Confidence.LOW),
        is_official_api=True,
    )
    assert low.risk_score > high.risk_score


def test_list_price_never_below_break_even(session):
    """Цена выставления не опускается ниже точки безубыточности."""
    seed_fee_schedules(session)
    # Рынок упал: floor 500, а купили за 1000.
    price = suggested_list_price(
        session,
        market=Market.TELEGRAM,
        cost_basis=Decimal("1000"),
        snapshot=_snapshot(median="500", floor="500"),
        markup=Decimal("0"),
        floor_ratio=Decimal("1.02"),
    )
    # При комиссии 20% безубыточность = 1000/0.8 = 1250, с маржой 2% = 1275.
    assert price >= Decimal("1275")


def test_cheaper_market_commission_changes_verdict(session):
    """Одна и та же сделка на разных площадках оценивается по-разному.

    Telegram удерживает около 20%, Portals — около 2,5%. Сделка,
    убыточная на первой, может быть прибыльной на второй.
    """
    seed_fee_schedules(session)
    common = dict(buy_price=Decimal("850"), snapshot=_snapshot(median="1000"))

    on_telegram = evaluate(
        session, buy_market=Market.TELEGRAM, sell_market=Market.TELEGRAM,
        is_official_api=True, **common,
    )
    on_portals = evaluate(
        session, buy_market=Market.PORTALS, sell_market=Market.PORTALS,
        is_official_api=False, **common,
    )

    assert on_telegram.net_profit < 0, "при комиссии 20% это убыток"
    assert on_portals.net_profit > on_telegram.net_profit
