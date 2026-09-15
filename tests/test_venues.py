"""Тесты сравнения цен между площадками.

Механика, которой не нужен прогноз: одна и та же модель стоит на
площадках по-разному. В разобранной выборке чужого бота Light Sword /
Enforcer стоил 7.20 на Portals, 7.42 и 8.79 на MRKT, 7.71 на Tonnel —
размах 22% на одной модели.

Здесь проверяется, что сравнение честное: сопоставляются одинаковые
модели, в одной валюте, и модель с одной площадки за разницу не
выдаётся.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import Capability, GiftRef, ListingDTO
from app.enums import Currency, Market
from app.services import marketdata, venues


@pytest.fixture()
def rate(session):
    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("100"), "test"
    )
    session.flush()


def _lot(market, model, price, currency=Currency.TON, ident="x"):
    return ListingDTO(
        market=market,
        external_id=ident,
        gift=GiftRef(collection="Light Sword", model=model),
        price=Decimal(str(price)),
        currency=currency,
    )


class _Adapter:
    """Площадка с заданным набором лотов."""

    def __init__(self, market, rows, fail=None):
        self.market = market
        self.rows = rows
        self.fail = fail

    def supports(self, _cap):
        return True

    async def search(self, **_kwargs):
        if self.fail:
            raise self.fail
        return self.rows


def _stub(monkeypatch, per_market):
    monkeypatch.setattr(
        venues, "get_adapter", lambda m: per_market[m]
    )
    monkeypatch.setattr(venues, "tradable_markets", lambda: list(per_market))


@pytest.mark.asyncio
async def test_cheapest_per_market_is_kept(session, rate, monkeypatch):
    """По каждой площадке берётся самый дешёвый лот модели."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [
            _lot(Market.PORTALS, "Enforcer", "7.20", ident="a"),
            _lot(Market.PORTALS, "Enforcer", "9.00", ident="b"),
        ]),
        Market.MRKT: _Adapter(Market.MRKT, [
            _lot(Market.MRKT, "Enforcer", "8.786", ident="c"),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")

    assert table["Enforcer"][Market.PORTALS].price == Decimal("7.20")
    assert table["Enforcer"][Market.MRKT].price == Decimal("8.786")


@pytest.mark.asyncio
async def test_spread_names_where_to_buy_and_sell(session, rate, monkeypatch):
    """Связка называет обе стороны и разницу между ними."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS,
                                 [_lot(Market.PORTALS, "Enforcer", "7.20")]),
        Market.MRKT: _Adapter(Market.MRKT,
                              [_lot(Market.MRKT, "Enforcer", "8.786")]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    rows = venues.spreads(table)

    assert len(rows) == 1
    assert rows[0]["buy_market"] is Market.PORTALS
    assert rows[0]["sell_market"] is Market.MRKT
    assert Decimal("0.21") < rows[0]["gap"] < Decimal("0.23")


@pytest.mark.asyncio
async def test_single_venue_model_is_not_a_spread(session, rate, monkeypatch):
    """Модель с одной площадки — это не «разницы нет», а «не с чем сравнить»."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [
            _lot(Market.PORTALS, "Enforcer", "7.20"),
            _lot(Market.PORTALS, "Windu", "6.91"),
        ]),
        Market.MRKT: _Adapter(Market.MRKT,
                              [_lot(Market.MRKT, "Enforcer", "8.00")]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    models = [row["model"] for row in venues.spreads(table)]

    assert models == ["Enforcer"]


@pytest.mark.asyncio
async def test_lots_without_a_model_are_skipped(session, rate, monkeypatch):
    """Лот без модели в сравнение не идёт.

    Цена внутри коллекции различается в разы именно из-за модели.
    """
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [_lot(Market.PORTALS, None, "7.20")]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")

    assert table == {}


@pytest.mark.asyncio
async def test_currencies_are_compared_in_stars(session, rate, monkeypatch):
    """Площадки в разных валютах сравниваются приведёнными к Stars.

    Иначе 700 Stars выглядели бы дороже 7 GRAM в сто раз, и курс
    выдавался бы за разницу цен.
    """
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS,
                                 [_lot(Market.PORTALS, "Enforcer", "7.00")]),
        Market.TELEGRAM: _Adapter(Market.TELEGRAM, [
            _lot(Market.TELEGRAM, "Enforcer", "770", currency=Currency.STARS),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    row = venues.spreads(table)[0]

    # 7 GRAM = 700 Stars против 770 Stars — десять процентов.
    assert row["buy_market"] is Market.PORTALS
    assert Decimal("0.09") < row["gap"] < Decimal("0.11")


@pytest.mark.asyncio
async def test_one_failing_venue_does_not_stop_the_rest(session, rate, monkeypatch):
    """Недоступная площадка не рушит сводку, а попадает в замечания."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS,
                                 [_lot(Market.PORTALS, "Enforcer", "7.20")]),
        Market.TONNEL: _Adapter(Market.TONNEL, [], fail=RuntimeError("cloudflare")),
    })

    table, notes = await venues.quotes_for(session, "Light Sword")

    assert "Enforcer" in table
    assert any("tonnel" in note for note in notes)


@pytest.mark.asyncio
async def test_missing_token_is_reported_not_hidden(session, rate, monkeypatch):
    """Площадка без токена называется прямо, а не исчезает из сводки."""

    class _NoSearch(_Adapter):
        def supports(self, _cap):
            return False

    _stub(monkeypatch, {
        Market.MRKT: _NoSearch(Market.MRKT, []),
    })

    _, notes = await venues.quotes_for(session, "Light Sword")

    assert any("токена" in note for note in notes)


# --- направление связки -------------------------------------------------


@pytest.mark.asyncio
async def test_direction_follows_the_money_not_the_price(
    session, rate, monkeypatch
):
    """Продаём не там, где дороже, а там, где после комиссий остаётся больше.

    Живые числа: Light Sword / Comics стоил 6.39 GRAM на MRKT и 761
    Stars в Telegram — размах 19%. Правило «продать где дороже» ведёт в
    Telegram, где комиссия 20%: 761 превращаются в 609, и сделка даёт
    минус. Portals дороже MRKT всего на 3%, но удерживает 2.5% — и
    остаётся ближе к нулю.
    """
    _stub(monkeypatch, {
        Market.MRKT: _Adapter(Market.MRKT,
                              [_lot(Market.MRKT, "Comics", "6.39")]),
        Market.PORTALS: _Adapter(Market.PORTALS,
                                 [_lot(Market.PORTALS, "Comics", "6.60")]),
        Market.TELEGRAM: _Adapter(Market.TELEGRAM, [
            _lot(Market.TELEGRAM, "Comics", "761", currency=Currency.STARS),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    row = venues.spreads(table, session)[0]

    assert row["buy_market"] is Market.MRKT
    assert row["sell_market"] is Market.PORTALS
    # Размах в строке — по выбранной паре, а не по самой дорогой.
    assert row["gap"] < Decimal("0.05")


@pytest.mark.asyncio
async def test_telegram_gap_is_its_commission(session, rate, monkeypatch):
    """Разница в сторону Telegram съедается его же комиссией.

    Это и есть причина, по которой таблица без комиссий читается
    неверно: 19% выглядят находкой, а после 20% удержания остаётся
    убыток.
    """
    _stub(monkeypatch, {
        Market.MRKT: _Adapter(Market.MRKT,
                              [_lot(Market.MRKT, "Comics", "6.39")]),
        Market.TELEGRAM: _Adapter(Market.TELEGRAM, [
            _lot(Market.TELEGRAM, "Comics", "761", currency=Currency.STARS),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    row = venues.spreads(table, session)[0]

    assert row["gap"] > Decimal("0.18")
    assert row["net_roi"] < 0


@pytest.mark.asyncio
async def test_profitable_pair_outranks_a_wider_gap(session, rate, monkeypatch):
    """Наверху таблицы — то, что остаётся, а не то, что шире.

    Строка с размахом 19% и убытком не должна стоять выше строки с
    размахом 25%, которая переживает комиссии.
    """
    _stub(monkeypatch, {
        Market.MRKT: _Adapter(Market.MRKT, [
            _lot(Market.MRKT, "Comics", "6.39", ident="a"),
            _lot(Market.MRKT, "Bifrost", "7.50", ident="b"),
        ]),
        Market.PORTALS: _Adapter(Market.PORTALS, [
            _lot(Market.PORTALS, "Bifrost", "6.00", ident="c"),
        ]),
        Market.TELEGRAM: _Adapter(Market.TELEGRAM, [
            _lot(Market.TELEGRAM, "Comics", "761", currency=Currency.STARS),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    rows = venues.spreads(table, session)

    assert rows[0]["model"] == "Bifrost"
    assert rows[0]["net_roi"] > 0
    assert rows[1]["net_roi"] < 0
    # Убыточная строка при этом шире по размаху — и всё равно ниже.
    assert rows[1]["gap"] > Decimal("0.18")


@pytest.mark.asyncio
async def test_without_a_session_the_gap_is_not_called_profit(
    session, rate, monkeypatch
):
    """Без комиссий показывается размах — и остаток не выдумывается."""
    _stub(monkeypatch, {
        Market.MRKT: _Adapter(Market.MRKT,
                              [_lot(Market.MRKT, "Comics", "6.39")]),
        Market.TELEGRAM: _Adapter(Market.TELEGRAM, [
            _lot(Market.TELEGRAM, "Comics", "761", currency=Currency.STARS),
        ]),
    })

    table, _ = await venues.quotes_for(session, "Light Sword")
    row = venues.spreads(table)[0]

    assert row["sell_market"] is Market.TELEGRAM
    assert row["net_roi"] is None


def test_sale_fee_rates_name_the_cost(session):
    """Комиссия продажи называется прямо: без неё таблицу не прочитать."""
    rates = venues.sale_fee_rates(session, [Market.TELEGRAM, Market.PORTALS])

    assert rates[Market.TELEGRAM] == Decimal("0.20")
    assert rates[Market.PORTALS] < rates[Market.TELEGRAM]
