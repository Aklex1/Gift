"""Тесты быстрого контура.

Недооценённый лот живёт секунды, а полный проход занимает минуты.
Контур смотрит только первую страницу по нескольким коллекциям — и
этим отличается от обычного прохода, который обходит весь рынок.

Главное, что проверяется, — что он остался **детектором, а не судьёй**.
Решение о покупке принимает тот же расчёт, что и раньше, со всеми
проверками; контур лишь решает, кого до него допустить. Если он начнёт
судить сам, он станет быстрым способом купить мусор.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.adapters.base import GiftRef, ListingDTO
from app.enums import Currency, Market
from app.models import Gift, Listing, MarketFact, utcnow
from app.services import fastlane, marketdata, runtime, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Общее хранилище и БД — на время теста свои."""
    from contextlib import contextmanager

    import app.db as db_module

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr("app.services.fastlane.session_scope", scope)
    store.invalidate()
    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("100"), "test"
    )
    session.flush()
    yield
    store.invalidate()


def _strategy(session, markets=("portals",), collections=()):
    from app.services import strategy as ss

    item = ss.create_strategy(session, name="s", budget_cap=Decimal("9000"))
    item.is_enabled = True
    item.markets = list(markets)
    item.collections = list(collections)
    session.flush()
    return item


def _dto(price="300", collection="Lol Pop", model="Orangery"):
    return ListingDTO(
        market=Market.PORTALS,
        external_id="x1",
        gift=GiftRef(collection=collection, model=model, slug="lolpop-1"),
        price=Decimal(price),
        currency=Currency.STARS,
    )


def _listing(session, price, collection="Lol Pop", model="Orangery", number=1):
    gift = Gift(
        canonical_key=f"{collection}#{number}", collection=collection,
        model=model, number=number,
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
    return gift


def _sale(session, price, collection="Lol Pop", model="Orangery",
          days_ago=1, ident="s1"):
    session.add(
        MarketFact(
            market=Market.FRAGMENT, collection=collection, model=model,
            price=Decimal(str(price)), currency=Currency.STARS,
            price_stars=Decimal(str(price)),
            happened_at=utcnow() - dt.timedelta(days=days_ago),
            suspected_wash=False, external_id=ident,
        )
    )
    session.flush()


# --- горячий список -----------------------------------------------------


def test_no_strategies_means_no_list(session):
    """Без включённых стратегий обходить нечего."""
    assert fastlane.hot_pairs(session, 10) == []


def test_strategy_collections_come_first(session):
    """Коллекции, названные в стратегии, — самое сильное указание."""
    _strategy(session, collections=["Lol Pop", "Candy Cane"])

    pairs = fastlane.hot_pairs(session, 10)

    assert [c for _, c in pairs][:2] == ["Lol Pop", "Candy Cane"]


def test_dispersion_feeds_the_list(session):
    """Коллекции с широким разбросом попадают в список сами."""
    _strategy(session)
    for i in range(10):
        _sale(session, 50 if i < 3 else 200, collection="Широкая",
              model="M", days_ago=i + 1, ident=f"w{i}")

    assert "Широкая" in [c for _, c in fastlane.hot_pairs(session, 10)]


def test_list_respects_its_size(session):
    """Список не разрастается: каждая пара — это запрос в каждый обход."""
    _strategy(session, collections=[f"К{i}" for i in range(50)])

    assert len(fastlane.hot_pairs(session, 5)) == 5


def test_only_markets_the_strategy_allows(session):
    """Обходим только площадки, где стратегия разрешила искать."""
    _strategy(session, markets=["portals"], collections=["Lol Pop"])

    assert {m for m, _ in fastlane.hot_pairs(session, 10)} == {Market.PORTALS}


# --- скрининг -----------------------------------------------------------


def test_cheap_lot_passes_the_screen(session):
    """Лот заметно ниже известной цены идёт в полную проверку."""
    _listing(session, 1000, number=1)

    assert fastlane.looks_cheap(
        session, _dto(price="300"), Decimal("300"), Decimal("0.15")
    )


def test_lot_at_the_going_rate_is_skipped(session):
    """Лот по рынку в полную проверку не идёт — на него и нет времени."""
    _listing(session, 1000, number=1)

    assert not fastlane.looks_cheap(
        session, _dto(price="1000"), Decimal("1000"), Decimal("0.15")
    )


def test_threshold_is_respected(session):
    """Порог решает, что считать заметным."""
    _listing(session, 1000, number=1)
    price = Decimal("900")  # ровно на 10% ниже

    assert not fastlane.looks_cheap(session, _dto(), price, Decimal("0.15"))
    assert fastlane.looks_cheap(session, _dto(), price, Decimal("0.05"))


def test_unknown_gift_is_let_through(session):
    """Подарок, о котором ничего не известно, пропускается дальше.

    Отбросить его значило бы не заметить находку в коллекции, куда
    сканер ещё не заходил. Таких единицы, и полная проверка их вывезет.
    """
    assert fastlane.looks_cheap(
        session, _dto(price="300"), Decimal("300"), Decimal("0.15")
    )


def test_screen_uses_no_margin(session):
    """Скрининг считает по честному потолку, без запаса дешёвой отсечки.

    Запас там нужен, чтобы не потерять находку. Здесь он только
    пропускал бы лишние лоты в дорогую проверку каждые двадцать секунд.
    """
    from app.services import scanner

    _listing(session, 1000, number=1)
    dto = _dto()

    assert scanner.cheap_ceiling(session, dto, margin=Decimal(1)) == Decimal(1000)
    assert scanner.cheap_ceiling(session, dto) > Decimal(1000)


# --- обход --------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_does_nothing_while_disabled(session):
    """Выключенный контур не ходит никуда."""
    _strategy(session, collections=["Lol Pop"])
    runtime.set_fast_lane(False)

    report = await fastlane.sweep()

    assert report["ok"] is False
    assert "выключен" in report["detail"]


@pytest.mark.asyncio
async def test_kill_switch_stops_the_sweep(session):
    """Kill switch останавливает и быстрый контур тоже."""
    _strategy(session, collections=["Lol Pop"])
    runtime.set_fast_lane(True)
    runtime.set_kill_switch(True)
    try:
        report = await fastlane.sweep()
    finally:
        runtime.set_kill_switch(False)

    assert report["ok"] is False
    assert "kill switch" in report["detail"]


@pytest.mark.asyncio
async def test_sweep_asks_one_page_per_collection(session, monkeypatch):
    """На коллекцию — один запрос первой страницы, не больше."""
    _strategy(session, collections=["Lol Pop", "Candy Cane"])
    runtime.set_fast_lane(True)
    asked: list[dict] = []

    class _Adapter:
        market = Market.PORTALS

        def supports(self, _cap):
            return True

        async def search(self, **kwargs):
            asked.append(kwargs)
            return []

    monkeypatch.setattr(
        "app.services.fastlane.get_adapter", lambda _m: _Adapter()
    )

    await fastlane.sweep()

    assert len(asked) == 2
    assert all(call["limit"] == fastlane.PAGE for call in asked)


@pytest.mark.asyncio
async def test_only_screened_lots_reach_the_full_check(session, monkeypatch):
    """В дорогую проверку уходят только те, кто прошёл скрининг.

    В этом вся экономия: страница отдаёт двадцать лотов, а считать
    полностью нужно единицы.
    """
    _strategy(session, collections=["Lol Pop"])
    runtime.set_fast_lane(True)
    _listing(session, 1000, number=1)

    rows = [_dto(price="300"), _dto(price="1000"), _dto(price="950")]
    for i, row in enumerate(rows):
        row.external_id = f"lot{i}"

    class _Adapter:
        market = Market.PORTALS

        def supports(self, _cap):
            return True

        async def search(self, **_kwargs):
            return rows

    evaluated: list[str] = []

    async def fake_evaluate(dto, _plan):
        evaluated.append(dto.external_id)
        from collections import Counter

        return (0, Counter())

    monkeypatch.setattr(
        "app.services.fastlane.get_adapter", lambda _m: _Adapter()
    )
    monkeypatch.setattr(
        "app.services.scanner.evaluate_listing", fake_evaluate
    )

    report = await fastlane.sweep()

    assert report["seen"] == 3
    assert evaluated == ["lot0"]
    assert report["screened"] == 1


@pytest.mark.asyncio
async def test_a_failing_collection_does_not_stop_the_rest(session, monkeypatch):
    """Промах по одной коллекции не рушит весь обход."""
    _strategy(session, collections=["Плохая", "Хорошая"])
    runtime.set_fast_lane(True)
    seen: list[str] = []

    class _Adapter:
        market = Market.PORTALS

        def supports(self, _cap):
            return True

        async def search(self, *, collection=None, **_kwargs):
            if collection == "Плохая":
                raise RuntimeError("площадка молчит")
            seen.append(collection)
            return []

    monkeypatch.setattr(
        "app.services.fastlane.get_adapter", lambda _m: _Adapter()
    )

    report = await fastlane.sweep()

    assert seen == ["Хорошая"]
    assert report["ok"] is True
    assert any("Плохая" in note for note in report["notes"])
