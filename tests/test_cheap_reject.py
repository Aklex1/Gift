"""Тесты дешёвой отсечки перед сетевыми запросами.

Замер с боевого прохода: 250 лотов, сбор 22 секунды, оценка 247.
Почти всё время — по одному запросу к Telegram на каждый лот. При этом
двести лотов из двухсот пятидесяти отсеивались как убыточные, и понять
это можно было по уже собранным данным.

Отсюда правило: сперва прикинуть потолок цены по тому, что лежит в базе
и в кэше, и идти в сеть только за теми лотами, которые при самой
щедрой оценке проходят порог.

Главное, что здесь проверяется, — что отсечка не съедает находки.
Ошибиться она может только в сторону лишней работы.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.adapters.base import GiftRef, ListingDTO
from app.enums import Currency, Market
from app.models import Gift, Listing, MarketFact, utcnow
from app.services import marketdata, scanner, valuation


@pytest.fixture()
def market(session):
    """Комиссии и курс, как в бою."""
    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("100"), "test"
    )
    valuation.seed_fee_schedules(session)
    session.flush()


def _dto(price="500", market_=Market.PORTALS):
    return ListingDTO(
        market=market_,
        external_id="x1",
        gift=GiftRef(collection="Lol Pop", model="Orangery", slug="lolpop-1"),
        price=Decimal(price),
        currency=Currency.STARS,
    )


def _listing(session, price, number=1):
    """Лот, который бот уже видел и сохранил."""
    gift = Gift(
        canonical_key=f"Lol Pop#{number}", collection="Lol Pop",
        model="Orangery", number=number,
    )
    session.add(gift)
    session.flush()
    session.add(
        Listing(
            market=Market.PORTALS, external_id=f"p{number}", gift_id=gift.id,
            price=Decimal(str(price)), currency=Currency.STARS,
            price_stars=Decimal(str(price)), is_active=True, seen_at=utcnow(),
        )
    )
    session.flush()


def _sale(session, price, days_ago=1, ident="s1"):
    session.add(
        MarketFact(
            market=Market.FRAGMENT, collection="Lol Pop", model="Orangery",
            price=Decimal(str(price)), currency=Currency.STARS,
            price_stars=Decimal(str(price)),
            happened_at=utcnow() - dt.timedelta(days=days_ago),
            suspected_wash=False, external_id=ident,
        )
    )
    session.flush()


# --- потолок ------------------------------------------------------------


def test_nothing_known_means_no_ceiling(session, market):
    """Без единого наблюдения судить не на чем."""
    assert scanner.cheap_ceiling(session, _dto()) is None


def test_ceiling_takes_the_most_generous_source(session, market):
    """Берётся максимум из известного, а не среднее и не минимум.

    Отсечка должна ошибаться в сторону лишней работы, а не молчаливой
    потери находки.
    """
    _listing(session, 400, number=1)
    _sale(session, 900, ident="a")

    ceiling = scanner.cheap_ceiling(session, _dto())

    assert ceiling is not None
    assert ceiling >= Decimal(900)


def test_ceiling_includes_a_margin(session, market):
    """К потолку добавляется запас на источники, которых ещё не видели."""
    _listing(session, 500)

    ceiling = scanner.cheap_ceiling(session, _dto())

    assert ceiling == Decimal(500) * scanner.CHEAP_MARGIN


def test_ceiling_asks_no_network(session, market, monkeypatch):
    """Потолок считается без единого обращения к площадкам.

    Иначе экономия превратилась бы в тот же самый запрос.
    """
    from app.adapters.registry import get_adapter

    adapter = get_adapter(Market.PORTALS)

    async def explode(*_a, **_k):
        raise AssertionError("отсечка полезла в сеть")

    monkeypatch.setattr(adapter, "attribute_floors", explode)
    monkeypatch.setattr(adapter, "request", explode)
    _listing(session, 500)

    assert scanner.cheap_ceiling(session, _dto()) is not None


# --- сама отсечка -------------------------------------------------------


def test_overpriced_lot_is_cut_without_network(session, market):
    """Лот дороже всего известного отсеивается сразу."""
    _listing(session, 400)
    _sale(session, 420, ident="b")

    assert scanner.hopeless(session, _dto(price="5000"), Decimal("5000"),
                            Decimal("0.1"))


def test_cheap_lot_goes_on_to_the_full_check(session, market):
    """Лот заметно ниже потолка отсечку проходит."""
    _listing(session, 1000)

    assert not scanner.hopeless(session, _dto(price="300"), Decimal("300"),
                                Decimal("0.1"))


def test_unknown_lot_is_never_cut(session, market):
    """Пока о подарке ничего не известно, отсекать его нельзя.

    Незнание — не основание для отказа: именно так теряются находки в
    коллекциях, куда сканер ещё не заходил.
    """
    assert not scanner.hopeless(session, _dto(price="5000"), Decimal("5000"),
                                Decimal("0.1"))


def test_cut_agrees_with_the_full_valuation(session, market):
    """Отсечка не отбрасывает то, что полная оценка бы приняла.

    Главное свойство: отсечка считает по завышенному потолку, поэтому
    её «безнадёжно» строго сильнее настоящего расчёта.
    """
    _listing(session, 1000)
    need = Decimal("0.1")

    for price in (200, 400, 600, 800, 900, 1000, 1200, 2000):
        price_stars = Decimal(price)
        cut = scanner.hopeless(session, _dto(price=str(price)), price_stars, need)
        if not cut:
            continue
        # Отсекли — значит и полный расчёт по самой щедрой цене не
        # дотянул бы до порога.
        snapshot = marketdata.snapshot_from_attribute_floor(
            collection="Lol Pop", model="Orangery",
            model_floor=Decimal(1000), listed_count=3,
        )
        result = valuation.evaluate(
            session, buy_market=Market.PORTALS, buy_price=price_stars,
            sell_market=Market.PORTALS, snapshot=snapshot,
            is_official_api=False, venue_floor=Decimal(1000),
        )
        assert result.net_roi < need, f"цена {price} отсеяна зря"


def test_margin_keeps_borderline_lots(session, market):
    """Лот у самой границы порога в сеть всё-таки уходит."""
    _listing(session, 1000)

    # Ровно по потолку без запаса: прибыли нет, но запас обязан
    # оставить его полной проверке.
    assert not scanner.hopeless(session, _dto(price="1000"), Decimal("1000"),
                                Decimal("0"))


# --- кэш официальных оценок ---------------------------------------------


class _Res:
    currency = "RUB"
    floor_price = 46800
    average_price = 105900
    value = 105900
    last_sale_price = None
    last_sale_date = None
    listed_count = 5
    value_is_average = False
    initial_sale_price = 23200
    initial_sale_stars = 176


def _adapter(counter):
    from app.adapters import telegram_mtproto as tm

    class _Gateway:
        async def call(self, _request):
            counter.append(1)
            return _Res()

    adapter = tm.TelegramAdapter.__new__(tm.TelegramAdapter)
    adapter._gateway = _Gateway()
    return adapter


@pytest.mark.asyncio
async def test_value_info_is_asked_once_per_gift():
    """Повторная оценка того же подарка берётся из памяти.

    Между проходами набор лотов почти не меняется, а запрос стоит
    секунду на каждый — из них и складывались четыре минуты прохода.
    """
    from app.adapters import telegram_mtproto as tm

    tm.forget_value_cache()
    calls: list[int] = []
    adapter = _adapter(calls)

    for _ in range(5):
        info = await tm.TelegramAdapter.value_info(adapter, "lolpop-1")

    assert len(calls) == 1
    assert info["floor_price"] == Decimal("468")


@pytest.mark.asyncio
async def test_different_gifts_are_asked_separately():
    """Кэш по подарку, а не один на всех."""
    from app.adapters import telegram_mtproto as tm

    tm.forget_value_cache()
    calls: list[int] = []
    adapter = _adapter(calls)

    await tm.TelegramAdapter.value_info(adapter, "lolpop-1")
    await tm.TelegramAdapter.value_info(adapter, "lolpop-2")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cache_can_be_dropped():
    """Накопленное можно сбросить — иначе не проверишь ничего живьём."""
    from app.adapters import telegram_mtproto as tm

    tm.forget_value_cache()
    calls: list[int] = []
    adapter = _adapter(calls)

    await tm.TelegramAdapter.value_info(adapter, "lolpop-1")
    tm.forget_value_cache()
    await tm.TelegramAdapter.value_info(adapter, "lolpop-1")

    assert len(calls) == 2
