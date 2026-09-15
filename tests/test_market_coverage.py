"""Тесты охвата выборки и скорости продаж.

Две вещи, от которых зависит, найдётся ли вообще что-нибудь: сколько
лотов проход успевает посмотреть и знает ли бот, как быстро такие лоты
уходят. Без скорости каждая сделка получает надбавку к риску
«скорость продаж неизвестна», и отбор строже, чем данные требуют.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Capability, CapabilityStatus, Currency, Market
from app.models import MarketFact, utcnow
from app.services import marketdata


# --- постраничный обход Portals ---------------------------------------


def _adapter(pages):
    """Адаптер Portals, отвечающий заданными страницами."""
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api")
    adapter.capabilities[Capability.SEARCH] = CapabilityStatus.EXPERIMENTAL
    asked: list[int] = []

    async def request(method, path, **kwargs):
        """Отдать страницу по смещению."""
        offset = kwargs["params"]["offset"]
        asked.append(offset)
        rows = pages.get(offset, [])
        return {"results": rows}

    adapter.request = request
    adapter.asked = asked
    return adapter


def _row(ident, price="1.5"):
    """Запись лота в формате площадки."""
    return {
        "id": str(ident),
        "price": price,
        "collection_name": "Lol Pop",
        "external_collection_number": 1,
    }


@pytest.mark.asyncio
async def test_walks_pages_until_limit():
    """За большой выборкой идём страницами, а не одним запросом."""
    adapter = _adapter({
        0: [_row(f"a{i}") for i in range(100)],
        100: [_row(f"b{i}") for i in range(100)],
        200: [_row(f"c{i}") for i in range(30)],
    })

    rows = await adapter.search(collection="Lol Pop", limit=500)

    assert adapter.asked == [0, 100, 200]
    assert len(rows) == 230


@pytest.mark.asyncio
async def test_stops_on_short_page():
    """Неполная страница означает конец выкладки."""
    adapter = _adapter({0: [_row(f"a{i}") for i in range(10)]})

    assert len(await adapter.search(collection="Lol Pop", limit=500)) == 10
    assert adapter.asked == [0]


@pytest.mark.asyncio
async def test_duplicates_between_pages_ignored():
    """Повтор между страницами не задваивает выборку.

    Площадка сдвигает выкладку, когда кто-то покупает лот прямо во
    время обхода, и одна запись попадает на две страницы.
    """
    same = [_row(f"same{i}") for i in range(100)]
    adapter = _adapter({0: same, 100: same, 200: same})

    rows = await adapter.search(collection="Lol Pop", limit=300)

    assert len(rows) == 100


@pytest.mark.asyncio
async def test_limit_respected():
    """Больше запрошенного не возвращаем."""
    adapter = _adapter({
        0: [_row(f"a{i}") for i in range(100)],
        100: [_row(f"b{i}") for i in range(100)],
    })

    assert len(await adapter.search(collection="Lol Pop", limit=150)) == 150


@pytest.mark.asyncio
async def test_empty_first_page():
    """Пустая выкладка — пустой результат, без лишних запросов."""
    adapter = _adapter({})

    assert await adapter.search(collection="Lol Pop", limit=500) == []
    assert adapter.asked == [0]


# --- скорость продаж из наблюдений ------------------------------------


def _fact(session, collection, model, days_ago=1, ident="x"):
    """Наблюдённая продажа."""
    session.add(
        MarketFact(
            market=Market.PORTALS,
            external_id=f"{collection}-{model}-{ident}",
            collection=collection,
            model=model,
            price=Decimal("10"),
            currency=Currency.TON,
            price_stars=Decimal("650"),
            happened_at=utcnow() - dt.timedelta(days=days_ago),
            suspected_wash=False,
        )
    )
    session.flush()


def _snapshot():
    """Срез по floor модели — в нём скорости нет."""
    return marketdata.snapshot_from_attribute_floor(
        collection="Lol Pop", model="Satellite",
        model_floor=Decimal("2000"), listed_count=8,
    )


def test_velocity_taken_from_facts(session):
    """Скорость подставляется из накопленных продаж."""
    for i in range(7):
        _fact(session, "Lol Pop", "Satellite", days_ago=i + 1, ident=str(i))

    filled = marketdata.with_observed_velocity(session, _snapshot())

    assert filled.velocity_per_day > 0
    assert filled.days_to_sell is not None


def test_no_facts_leaves_velocity_unknown(session):
    """Без наблюдений скорость остаётся неизвестной, а не выдумывается."""
    filled = marketdata.with_observed_velocity(session, _snapshot())

    assert filled.velocity_per_day == 0.0
    assert filled.days_to_sell is None


def test_other_model_does_not_count(session):
    """Продажи другой модели к этой не относятся."""
    _fact(session, "Lol Pop", "Другая")

    assert marketdata.with_observed_velocity(
        session, _snapshot()
    ).velocity_per_day == 0.0


def test_wash_trades_excluded(session):
    """Подозрительные сделки в скорость не идут."""
    _fact(session, "Lol Pop", "Satellite")
    session.query(MarketFact).update({"suspected_wash": True})
    session.flush()

    assert marketdata.with_observed_velocity(
        session, _snapshot()
    ).velocity_per_day == 0.0


def test_existing_velocity_kept(session):
    """Уже известную скорость не перетираем."""
    snapshot = _snapshot()
    snapshot.velocity_per_day = 5.0

    assert marketdata.with_observed_velocity(
        session, snapshot
    ).velocity_per_day == 5.0


# --- сведение источников по одному подарку ----------------------------


def _dto(collection="Lol Pop", model="Satellite", market=Market.PORTALS):
    """Лот для оценки."""
    from app.adapters.base import GiftRef, ListingDTO

    return ListingDTO(
        market=market,
        external_id="1",
        gift=GiftRef(collection=collection, model=model, slug="lolpop-1"),
        price=Decimal("10"),
        currency=Currency.TON,
    )


def test_primary_prefers_model_floor():
    """Floor модели точнее всего для редкой модели — он и основной."""
    from app.services import scanner

    floor = marketdata.snapshot_from_attribute_floor(
        collection="Lol Pop", model="Satellite",
        model_floor=Decimal("2000"), listed_count=8,
    )
    telegram = marketdata.snapshot_from_telegram(
        {"floor_price": Decimal("1500"), "average_price": Decimal("1800")},
        collection="Lol Pop", model="Satellite",
    )

    chosen = scanner.choose_primary([telegram, floor], Market.PORTALS)

    assert chosen.source == "portals_attribute_floor"


def test_primary_falls_back_to_telegram():
    """Без floor модели берётся официальная оценка Telegram."""
    from app.services import scanner

    telegram = marketdata.snapshot_from_telegram(
        {"floor_price": Decimal("1500"), "average_price": Decimal("1800")},
        collection="Lol Pop", model="Satellite",
    )

    chosen = scanner.choose_primary([telegram], Market.TELEGRAM)

    assert chosen.source == "telegram_value_info"


def test_primary_of_nothing_is_none():
    """Без источников основного среза нет — и выдумывать его нельзя."""
    from app.services import scanner

    assert scanner.choose_primary([], Market.PORTALS) is None


def test_audit_view_lists_every_source():
    """В сводке видно, что сказал каждый источник."""
    from app.services import scanner

    floor = marketdata.snapshot_from_attribute_floor(
        collection="Lol Pop", model="Satellite",
        model_floor=Decimal("2000"), listed_count=8,
    )
    telegram = marketdata.snapshot_from_telegram(
        {"floor_price": Decimal("1500"), "average_price": Decimal("1800")},
        collection="Lol Pop", model="Satellite",
    )

    view = scanner.audit_view([floor, telegram])

    assert len(view) == 2
    assert {row["source"] for row in view} == {
        "portals_attribute_floor", "telegram_value_info"
    }
    assert view[0]["floor"] == "2000"
    assert view[1]["median"] == "1800"


def test_audit_view_keeps_disagreement_visible():
    """Расхождение источников не сглаживается — в этом весь смысл сводки.

    Усреднять цены разных площадок нельзя: получилось бы число, по
    которому нельзя ни купить, ни продать.
    """
    from app.services import scanner

    a = marketdata.snapshot_from_attribute_floor(
        collection="C", model="M", model_floor=Decimal("2000"), listed_count=3
    )
    b = marketdata.snapshot_from_telegram(
        {"floor_price": Decimal("900")}, collection="C", model="M"
    )

    floors = [row["floor"] for row in scanner.audit_view([a, b])]

    assert floors == ["2000", "900"]


# --- цены, названные самими площадками ---------------------------------


def test_live_prices_name_their_market():
    """Каждая цена приписывается той площадке, что её назвала."""
    from app.services import scanner

    floor = marketdata.snapshot_from_attribute_floor(
        collection="Lol Pop", model="Satellite",
        model_floor=Decimal("2000"), listed_count=8,
    )
    telegram = marketdata.snapshot_from_telegram(
        {"floor_price": Decimal("1500"), "average_price": Decimal("1800")},
        collection="Lol Pop", model="Satellite",
    )

    prices = scanner.live_prices([floor, telegram])

    assert prices == {
        Market.PORTALS: Decimal("2000"),
        Market.TELEGRAM: Decimal("1500"),
    }


def test_own_sample_is_not_a_market_price(session):
    """Собственная выборка ничьей ценой не считается.

    Она сводит наблюдения по всем площадкам сразу, и выдать её за цену
    конкретного рынка значило бы нарисовать прибыль на чужих числах.
    """
    from app.services import scanner

    own = marketdata.snapshot_for(session, collection="Lol Pop", model="Satellite")

    assert scanner.live_prices([own]) == {}


def test_value_info_in_ton_converted_to_stars(session, monkeypatch):
    """Оценка Telegram в TON переводится в Stars.

    Резейл всё чаще считает в TON. Без пересчёта floor в 5 TON встал бы
    рядом с ценой лота в Stars, и подарок выглядел бы в десятки раз
    дешевле рынка — ровно тот перекос, из-за которого отбор врёт.
    """
    from app.services import scanner

    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )

    out = scanner._value_info_in_stars(session, {
        "currency": Currency.TON,
        "floor_price": Decimal("5"),
        "average_price": Decimal("6"),
        "last_sale_date": None,
    })

    assert out["currency"] is Currency.STARS
    assert out["floor_price"] == Decimal("325")
    assert out["average_price"] == Decimal("390")


def test_value_info_in_stars_untouched(session):
    """Оценка уже в Stars проходит как есть."""
    from app.services import scanner

    info = {"currency": Currency.STARS, "floor_price": Decimal("1500")}

    assert scanner._value_info_in_stars(session, info) == info


def test_value_info_dropped_without_rate(session, monkeypatch):
    """Без курса источник отбрасывается целиком.

    Половина цифр в Stars, половина в TON — хуже, чем их отсутствие.
    """
    from app.services import scanner

    monkeypatch.setattr(marketdata, "to_stars", lambda *_a, **_k: None)

    out = scanner._value_info_in_stars(
        session, {"currency": Currency.TON, "floor_price": Decimal("5")}
    )

    assert out is None
