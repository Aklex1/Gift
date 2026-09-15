"""Тесты разброса цен и отбора по разрыву.

Разброс отвечает на вопрос, который нельзя задать floor'у: бывают ли в
этом виде подарков дешёвые входы вообще. Там, где все сделки в узкой
полосе, недооценённому лоту взяться неоткуда; там, где медиана вдвое
выше дешёвого хвоста, дешёвые покупки случаются регулярно.

Главное, что здесь проверяется, — что показатель не выдумывается:
мало сделок, чужая площадка, другая модель — всё это поводы промолчать,
а не назвать число.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Currency, Market
from app.models import MarketFact, utcnow
from app.services import salestats


def _sale(
    session,
    price,
    *,
    collection="Chill Flame",
    model="Lego",
    days_ago=1,
    market=Market.FRAGMENT,
    wash=False,
    ident=None,
):
    """Состоявшаяся сделка."""
    session.add(
        MarketFact(
            market=market,
            collection=collection,
            model=model,
            price=Decimal(str(price)),
            currency=Currency.STARS,
            price_stars=Decimal(str(price)),
            happened_at=utcnow() - dt.timedelta(days=days_ago),
            suspected_wash=wash,
            external_id=ident or f"{collection}-{model}-{price}-{days_ago}",
        )
    )
    session.flush()


def _stats(session, **kwargs):
    return salestats.sale_stats(
        session, collection="Chill Flame", model="Lego", **kwargs
    )


# --- сама статистика ---------------------------------------------------


def test_percentiles_come_from_real_payments():
    """Процентиль — реально состоявшийся платёж, без интерполяции.

    Интерполированная цена — цена, по которой никто не покупал, а
    опираться нужно на то, что случилось.
    """
    values = [Decimal(n) for n in (10, 20, 30, 40, 100)]

    assert salestats.percentile(values, 0.0) == Decimal(10)
    assert salestats.percentile(values, 0.5) == Decimal(30)
    assert salestats.percentile(values, 1.0) == Decimal(100)
    assert salestats.percentile([], 0.5) is None


def test_spread_is_median_over_cheap_tail(session):
    """Разброс — во сколько раз медиана выше дешёвого хвоста."""
    for i, price in enumerate((40, 50, 80, 100, 100, 120, 150)):
        _sale(session, price, days_ago=i + 1, ident=f"s{i}")

    stats = _stats(session)

    assert stats.sales == 7
    assert stats.median == Decimal(100)
    assert stats.low == Decimal(40)
    assert stats.spread == 2.5


def test_no_spread_on_a_thin_sample(session):
    """По трём сделкам разброс не считается.

    Одна случайная дешёвая продажа управляла бы всем показателем.
    """
    for i, price in enumerate((40, 100, 150)):
        _sale(session, price, days_ago=i + 1, ident=f"t{i}")

    stats = _stats(session)

    assert stats.sales == 3
    assert not stats.reliable
    assert stats.spread is None


def test_narrow_band_gives_low_spread(session):
    """Плотный рынок — разброс около единицы, и это честный ответ."""
    for i, price in enumerate((98, 99, 100, 100, 101, 102)):
        _sale(session, price, days_ago=i + 1)

    assert _stats(session).spread < 1.1


def test_other_market_does_not_count(session):
    """Сделки другой площадки в разброс не идут.

    Один и тот же подарок на Fragment и в Telegram стоит по-разному.
    Сведённая выборка показала бы разброс там, где есть только разница
    площадок, — и он выглядел бы как возможность.
    """
    for i, price in enumerate((40, 50, 80, 100, 120, 150)):
        _sale(session, price, days_ago=i + 1, market=Market.TELEGRAM)

    assert _stats(session).sales == 0


def test_other_model_does_not_count(session):
    """Разброс внутри коллекции — это почти целиком разница моделей."""
    for i, price in enumerate((40, 50, 80, 100, 120, 150)):
        _sale(session, price, model="Другая", days_ago=i + 1)

    assert _stats(session).sales == 0


def test_wash_trades_excluded(session):
    """Накрутка объёма не должна двигать ни медиану, ни скорость."""
    for i, price in enumerate((100, 100, 100, 100, 100)):
        _sale(session, price, days_ago=i + 1)
    _sale(session, 5, days_ago=1, wash=True, ident="wash")

    stats = _stats(session)

    assert stats.sales == 5
    assert stats.low == Decimal(100)


def test_old_sales_fall_out_of_the_window(session):
    """Цены, которых на рынке уже нет, в расчёт не идут."""
    for i in range(6):
        _sale(session, 100, days_ago=90 + i)

    assert _stats(session).sales == 0


def test_velocity_is_sales_per_day(session):
    """Скорость — сделок в день за окно наблюдения."""
    for i in range(30):
        _sale(session, 100, days_ago=i % 29 + 1, ident=f"s{i}")

    assert _stats(session).velocity_per_day == pytest.approx(1.0, abs=0.01)


def test_nothing_known_is_not_zero(session):
    """Без сделок показателей нет — ни нулевых, ни каких-либо."""
    stats = _stats(session)

    assert stats.sales == 0
    assert stats.median is None
    assert stats.spread is None


# --- угодья ------------------------------------------------------------


def test_hunting_grounds_rank_by_spread_and_speed(session):
    """Вперёд выходит то, где полоса шире И торгуют чаще.

    Широкий разброс без сделок — мёртвая коллекция: ловить там можно
    месяцами. Поэтому одного разброса для верхней строчки мало.
    """
    # Разброс втрое, но всего шесть сделок за месяц.
    for i, price in enumerate((10, 20, 30, 40, 50, 50)):
        _sale(session, price, collection="Редкая", model="M",
              days_ago=i + 1, ident=f"r{i}")
    # Разброс вдвое, зато сделка в день.
    for i in range(30):
        price = 50 if i < 3 else 100
        _sale(session, price, collection="Бойкая", model="M",
              days_ago=i % 29 + 1, ident=f"b{i}")

    grounds = salestats.hunting_grounds(session)

    assert grounds[0]["collection"] == "Бойкая"
    assert grounds[0]["spread"] == 2.0
    # Редкая не отбрасывается — она просто ниже.
    assert grounds[1]["collection"] == "Редкая"
    assert grounds[1]["spread"] == 3.0


def test_hunting_grounds_skip_thin_samples(session):
    """Коллекция с парой сделок в угодья не попадает."""
    _sale(session, 10, collection="Тонкая", model="M", days_ago=1)
    _sale(session, 100, collection="Тонкая", model="M", days_ago=2)

    assert salestats.hunting_grounds(session) == []


# --- разрыв кандидата --------------------------------------------------


def test_gap_is_distance_from_fair_value():
    """Разрыв — насколько лот дешевле справедливой цены."""
    from app.services.scanner import price_gap

    assert price_gap(Decimal(100), Decimal(40)) == Decimal("0.6")


def test_gap_is_negative_when_overpriced():
    """Лот дороже оценки даёт отрицательный разрыв, а не нулевой."""
    from app.services.scanner import price_gap

    assert price_gap(Decimal(100), Decimal(120)) == Decimal("-0.2")


def test_gap_unknown_without_fair_value():
    """Без справедливой цены разрыв не считается."""
    from app.services.scanner import price_gap

    assert price_gap(Decimal(0), Decimal(40)) is None


# --- откуда берётся справедливая цена ----------------------------------


def test_fragment_outranks_own_sample_but_not_the_market():
    """Порядок источников: площадки впереди, Fragment — впереди своих.

    Сделки Fragment честнее по природе (по ним заплатили), но сняты с
    другой площадки, где подарок обычно дешевле. Поставить их выше
    оценки самой площадки значило бы занижать справедливую цену; выше
    собственной выборки — нет, там всего пара наблюдений.
    """
    from app.services import marketdata, scanner

    fragment = marketdata.MarketSnapshot(
        collection="C", model="M", median_price=Decimal(40),
        floor_price=None, sample_size=60,
        confidence=marketdata.Confidence.HIGH, velocity_per_day=2.0,
        newest=utcnow(), active_listings=0, source="market:fragment",
    )
    own = marketdata.MarketSnapshot(
        collection="C", model="M", median_price=Decimal(90),
        floor_price=None, sample_size=2,
        confidence=marketdata.Confidence.LOW, velocity_per_day=0.0,
        newest=utcnow(), active_listings=1, source="own",
    )
    floor = marketdata.snapshot_from_attribute_floor(
        collection="C", model="M", model_floor=Decimal(100), listed_count=5
    )

    assert scanner.choose_primary([fragment, own], Market.PORTALS) is fragment
    assert scanner.choose_primary([own, fragment], Market.PORTALS) is fragment
    assert scanner.choose_primary([fragment, floor], Market.PORTALS) is floor
