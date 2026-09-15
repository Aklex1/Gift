"""Тесты поиска разницы цен между площадками.

Главное, что проверяется: связка признаётся выгодной только после
обеих комиссий, переноса подарка и приведения валют, а ориентиром
продажи служит текущий минимум целевой площадки, а не медиана.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import GiftRef, ListingDTO
from app.enums import Currency, Market
from app.services import arbitrage


@pytest.fixture(autouse=True)
def isolated_store(session, monkeypatch):
    """Подменить хранилище настроек и курс на тестовые."""
    from contextlib import contextmanager

    from app.services import store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()
    # Фиксированный курс: тест не должен зависеть от внешнего источника.
    monkeypatch.setattr(
        "app.services.marketdata.to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )
    yield
    store.invalidate()


def listing(market, price, *, model="neon", collection="pepe", ext="1", currency=None):
    """Лот с заданной ценой в валюте площадки."""
    if currency is None:
        currency = Currency.STARS if market is Market.TELEGRAM else Currency.TON
    return ListingDTO(
        market=market,
        external_id=ext,
        gift=GiftRef(collection=collection, model=model, number=int(ext)),
        price=Decimal(str(price)),
        currency=currency,
    )


# --- ключ сопоставимого товара ---------------------------------------


def test_kind_key_needs_model():
    """Без модели сравнивать нечего: цены внутри коллекции разнятся в разы."""
    assert arbitrage.kind_key(listing(Market.PORTALS, 10)) == "pepe|neon"
    assert arbitrage.kind_key(listing(Market.PORTALS, 10, model="")) is None


def test_kind_key_is_case_insensitive():
    """Площадки пишут названия по-разному — ключ это переживает."""
    a = arbitrage.kind_key(listing(Market.PORTALS, 10, model="Neon", collection="Pepe"))
    b = arbitrage.kind_key(listing(Market.MRKT, 10, model="neon", collection="pepe"))
    assert a == b


# --- индекс цен -------------------------------------------------------


def test_index_keeps_cheapest_per_market(session):
    """У площадки запоминается самое дешёвое предложение."""
    rows = [
        listing(Market.PORTALS, 10, ext="1"),
        listing(Market.PORTALS, 7, ext="2"),
        listing(Market.PORTALS, 12, ext="3"),
    ]
    index = arbitrage.build_index(session, rows)
    quote = index["pepe|neon"][Market.PORTALS]

    assert quote.price_native == Decimal("7")
    assert quote.external_id == "2"
    assert quote.listings == 3


def test_index_converts_to_stars(session):
    """Цены разных валют сравниваются через курс, а не напрямую."""
    rows = [
        listing(Market.PORTALS, 1, ext="1"),
        listing(Market.TELEGRAM, 50, ext="2"),
    ]
    index = arbitrage.build_index(session, rows)["pepe|neon"]

    # 1 TON = 65 Stars, поэтому Portals дороже, хотя число меньше.
    assert index[Market.PORTALS].price_stars == Decimal("65")
    assert index[Market.TELEGRAM].price_stars == Decimal("50")


# --- поиск связок -----------------------------------------------------


def test_equal_prices_give_nothing(session):
    """Одинаковые цены — не возможность."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 10, ext="2")]
    assert arbitrage.find(session, rows) == []


def test_small_spread_does_not_survive_fees(session):
    """Разница в пару процентов съедается комиссиями и переносом."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 10.3, ext="2")]
    assert arbitrage.find(session, rows) == []


def test_large_spread_is_found(session):
    """Существенная разница переживает издержки и попадает в результат."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]
    found = arbitrage.find(session, rows)

    assert len(found) == 1
    spread = found[0]
    assert spread.buy.market is Market.PORTALS
    assert spread.sell.market is Market.MRKT
    assert spread.net_profit > 0
    assert spread.net_roi >= arbitrage.DEFAULT_MIN_ROI


def test_direction_is_cheap_to_expensive(session):
    """Обратное направление не предлагается: продавать дешевле бессмысленно."""
    rows = [listing(Market.MRKT, 16, ext="1"), listing(Market.PORTALS, 10, ext="2")]
    found = arbitrage.find(session, rows)

    assert all(s.buy.market is Market.PORTALS for s in found)


def test_transfer_cost_is_subtracted(session):
    """Перенос подарка учитывается: с дорогим переносом связки нет."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]
    assert arbitrage.find(session, rows)

    from app.services import store

    store.set(arbitrage.KEY_TRANSFER_TON, "50")
    assert arbitrage.find(session, rows) == []


def test_single_market_is_not_arbitrage(session):
    """Одна площадка — это не межплощадочная разница."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.PORTALS, 30, ext="2")]
    assert arbitrage.find(session, rows) == []


def test_different_models_are_not_compared(session):
    """Разные модели — разные товары, и сравнивать их цены нельзя."""
    rows = [
        listing(Market.PORTALS, 10, model="common", ext="1"),
        listing(Market.MRKT, 90, model="rare", ext="2"),
    ]
    assert arbitrage.find(session, rows) == []


def test_sale_anchor_is_lowest_ask_not_highest(session):
    """Ориентир продажи — минимум целевой площадки, а не лучший лот."""
    rows = [
        listing(Market.PORTALS, 10, ext="1"),
        listing(Market.MRKT, 16, ext="2"),
        listing(Market.MRKT, 40, ext="3"),
    ]
    found = arbitrage.find(session, rows)

    assert len(found) == 1
    # Считали по 16, а не по 40: иначе прибыль была бы нарисованной.
    assert found[0].sell.price_native == Decimal("16")


def test_thin_market_is_flagged(session):
    """Единственный лот на целевой площадке — повод предупредить."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]
    spread = arbitrage.find(session, rows)[0]

    assert any("случайной" in reason for reason in spread.reasons)


def test_results_sorted_by_roi(session):
    """Лучшие связки идут первыми."""
    rows = [
        listing(Market.PORTALS, 10, model="a", ext="1"),
        listing(Market.MRKT, 16, model="a", ext="2"),
        listing(Market.PORTALS, 10, model="b", ext="3"),
        listing(Market.MRKT, 30, model="b", ext="4"),
    ]
    found = arbitrage.find(session, rows)

    assert len(found) == 2
    assert found[0].net_roi > found[1].net_roi
    assert found[0].kind == "pepe|b"


def test_markets_filter_respected(session):
    """Можно ограничить поиск конкретными площадками."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]

    assert arbitrage.find(session, rows, markets={Market.PORTALS}) == []
    assert arbitrage.find(session, rows, markets={Market.PORTALS, Market.MRKT})


def test_threshold_filters_results(session):
    """Порог доходности отсекает слабые связки."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]

    assert arbitrage.find(session, rows, threshold=Decimal("0.01"))
    assert arbitrage.find(session, rows, threshold=Decimal("5")) == []


# --- включение --------------------------------------------------------


def test_disabled_by_default():
    """Связка требует ручного переноса — сама она не включается."""
    assert arbitrage.enabled() is False


def test_can_be_enabled():
    """Владелец может включить поиск явно."""
    arbitrage.set_enabled(True)
    assert arbitrage.enabled() is True
    arbitrage.set_enabled(False)
    assert arbitrage.enabled() is False


def test_spread_report_is_readable(session):
    """Отчёт содержит обе стороны сделки и понятен без кода."""
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]
    data = arbitrage.find(session, rows)[0].as_dict()

    assert data["buy_market"] == "portals"
    assert data["sell_market"] == "mrkt"
    # Человеку валюта показывается под нынешним именем: TON внутри — GRAM снаружи.
    assert "GRAM" in data["buy_price_native"]
    assert "TON" not in data["buy_price_native"]
    assert data["net_roi"].endswith("%")
    assert any("не атомарна" in r for r in data["reasons"])


def test_network_fees_counted_in_stars(session):
    """Сетевые комиссии площадок заданы в TON и должны быть переведены.

    Без перевода 0.05 TON считались как 0.05 Stars, и связка выглядела
    прибыльнее, чем есть, — ровно на величину, которую съедает сеть.
    """
    from app.services import marketdata as md
    from app.services.valuation import seed_fee_schedules

    seed_fee_schedules(session)
    rows = [listing(Market.PORTALS, 10, ext="1"), listing(Market.MRKT, 16, ext="2")]
    spread = arbitrage.find(session, rows, threshold=Decimal("0"))[0]

    rate = Decimal("65")
    buy_network = md.to_stars(session, Decimal("0.05"), Currency.TON)
    sell_network = md.to_stars(session, Decimal("0.1"), Currency.TON)
    transfer = md.to_stars(session, arbitrage.transfer_cost_ton(), Currency.TON)

    expected_cost = 10 * rate + buy_network + transfer
    expected_proceeds = 16 * rate * Decimal("0.95") - sell_network

    assert spread.total_cost == expected_cost
    assert spread.net_proceeds == expected_proceeds
    assert spread.net_profit == expected_proceeds - expected_cost
