"""Тесты: цена одной площадки не выдаётся за цену другой.

Живой случай, с которого это началось. Лот Candy Cane #125394
(Goldium) в Telegram за 480 ★. Источники сказали разное:

* официальная оценка Telegram — медиана 840 ★, floor 571 ★;
* floor модели Goldium на Portals — 2640 ★.

Основным брался floor с Portals — безусловно, даже для телеграмного
лота. Справедливой ценой становились 2640 ★, продажа планировалась по
3036 ★ при floor площадки 571 ★, и ROI выходил 406%. Продать по такой
цене в Telegram нельзя: там рядом стоят лоты по 571 ★.

Отсюда два правила, которые здесь и проверяются: основной источник —
той площадки, где торгуем; а цена продажи не выше самого дешёвого
конкурента на ней.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Confidence, Currency, Market
from app.services import marketdata, scanner, valuation


def _telegram_snapshot(median="840", floor="571"):
    """Официальная оценка Telegram по коллекции."""
    return marketdata.snapshot_from_telegram(
        {
            "floor_price": Decimal(floor),
            "average_price": Decimal(median),
            "listed_count": 12045,
        },
        collection="Candy Cane",
        model="Goldium",
    )


def _portals_floor(value="2640"):
    """Floor модели на Portals — цена на Portals, не в Telegram."""
    return marketdata.snapshot_from_attribute_floor(
        collection="Candy Cane",
        model="Goldium",
        model_floor=Decimal(value),
        listed_count=4,
        models_listed=50,
    )


# --- выбор основного источника ----------------------------------------


def test_telegram_lot_valued_by_telegram():
    """Лот в Telegram оценивается оценкой Telegram, не ценой Portals."""
    chosen = scanner.choose_primary(
        [_telegram_snapshot(), _portals_floor()], Market.TELEGRAM
    )

    assert chosen.source == "telegram_value_info"


def test_portals_lot_valued_by_portals():
    """А лот на Portals — floor'ом модели с Portals. Симметрично."""
    chosen = scanner.choose_primary(
        [_telegram_snapshot(), _portals_floor()], Market.PORTALS
    )

    assert chosen.source == "portals_attribute_floor"


def test_order_of_sources_does_not_decide():
    """Порядок сбора источников на выбор не влияет."""
    sources = [_portals_floor(), _telegram_snapshot()]

    assert scanner.choose_primary(sources, Market.TELEGRAM).source == (
        "telegram_value_info"
    )


def test_foreign_price_still_better_than_nothing():
    """Без своего источника чужой берётся — но как запасной.

    Совсем без оценки лот не рассмотреть, а завышение ловится
    потолком площадки при расчёте продажи.
    """
    chosen = scanner.choose_primary([_portals_floor()], Market.TELEGRAM)

    assert chosen.source == "portals_attribute_floor"


# --- потолок площадки --------------------------------------------------


def test_venue_floor_taken_from_its_own_source():
    """Потолок берётся у источника той площадки, где продаём."""
    sources = [_telegram_snapshot(), _portals_floor()]

    assert scanner.venue_floor(sources, Market.TELEGRAM) == Decimal("571")
    assert scanner.venue_floor(sources, Market.PORTALS) == Decimal("2640")


def test_no_venue_floor_without_its_source():
    """Нет источника площадки — нет и потолка, выдумывать нечего."""
    assert scanner.venue_floor([_portals_floor()], Market.MRKT) is None


@pytest.fixture()
def fees(session, monkeypatch):
    """Комиссии по умолчанию и фиксированный курс."""
    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )
    valuation.seed_fee_schedules(session)
    session.flush()


def test_foreign_estimate_is_capped_by_the_venue(session, fees):
    """Чужая оценка не обещает продажу по цене, которой тут нет.

    Ровно тот случай: оценка 2640 ★ с Portals, а в Telegram такие
    лоты стоят от 571 ★. Без потолка ROI выходил 406%.
    """
    result = valuation.evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("480"),
        sell_market=Market.TELEGRAM,
        snapshot=_portals_floor(),
        is_official_api=True,
        venue_floor=Decimal("571"),
    )

    assert result.expected_sale_price == Decimal("571")
    # Комиссия Telegram 20%: 571 × 0.8 = 457 против 480 затрат.
    assert result.net_roi < 0
    assert any("571" in reason for reason in result.reasons)


def test_without_the_cap_the_roi_is_fantasy(session, fees):
    """Тот же расчёт без потолка — та самая нереальная доходность.

    Тест закрепляет, что дело было именно в потолке, а не в чём-то
    ещё: убираем его и получаем обратно кратный ROI.
    """
    result = valuation.evaluate(
        session,
        buy_market=Market.TELEGRAM,
        buy_price=Decimal("480"),
        sell_market=Market.TELEGRAM,
        snapshot=_portals_floor(),
        is_official_api=True,
    )

    assert result.net_roi > Decimal("3")


def test_cap_does_not_touch_an_honest_deal(session, fees):
    """Когда оценка своя и ниже потолка, потолок ничего не меняет."""
    snapshot = _telegram_snapshot(median="840", floor="571")

    with_cap = valuation.evaluate(
        session,
        buy_market=Market.TELEGRAM, buy_price=Decimal("300"),
        sell_market=Market.TELEGRAM, snapshot=snapshot,
        is_official_api=True, venue_floor=Decimal("571"),
    )
    without = valuation.evaluate(
        session,
        buy_market=Market.TELEGRAM, buy_price=Decimal("300"),
        sell_market=Market.TELEGRAM, snapshot=snapshot,
        is_official_api=True,
    )

    assert with_cap.net_roi == without.net_roi
    assert with_cap.expected_sale_price == Decimal("571")


def test_cap_never_raises_the_price(session, fees):
    """Потолок только опускает цену продажи, но не поднимает."""
    result = valuation.evaluate(
        session,
        buy_market=Market.TELEGRAM, buy_price=Decimal("300"),
        sell_market=Market.TELEGRAM,
        snapshot=_telegram_snapshot(median="840", floor="571"),
        is_official_api=True,
        venue_floor=Decimal("9000"),
    )

    assert result.expected_sale_price == Decimal("571")


# --- расхождение источников -------------------------------------------
#
# Второй живой случай. Chill Flame #114734 за 504 ★, и три источника:
#
#   telegram_value_info      медиана 423 600, floor 198 800
#   portals_attribute_floor  1 676
#   own                      504
#
# ROI выходил 36 188%. Рынком это не объясняется: floor коллекции по
# определению не выше floor её модели, а тут он выше в 118 раз. Значит
# сломан масштаб — единица измерения, курс или валюта. Число при этом
# выглядит тем убедительнее, чем сильнее поломка.


def _priced(source, value):
    """Срез, который называет одну цену."""
    return marketdata.MarketSnapshot(
        collection="Chill Flame", model="Oil Lamp",
        median_price=Decimal(value), floor_price=Decimal(value),
        sample_size=5, confidence=Confidence.HIGH, velocity_per_day=0.0,
        newest=None, active_listings=2, source=source,
    )


def test_disagreement_is_measured_between_extremes():
    """Считается разрыв между самым дешёвым и самым дорогим источником."""
    gap = scanner.source_disagreement([
        _priced("telegram_value_info", "198800"),
        _priced("portals_attribute_floor", "1676"),
        _priced("own", "504"),
    ])

    assert gap is not None
    times, low, high = gap
    assert low.source == "own"
    assert high.source == "telegram_value_info"
    assert times > scanner.MAX_SOURCE_DISAGREEMENT


def test_ordinary_difference_is_not_a_disagreement():
    """Разница в разы — обычная жизнь, а не поломка.

    Оценка Telegram считается по всей коллекции, floor на Portals — по
    модели, и редкая модель стоит кратно дороже рядовой.
    """
    gap = scanner.source_disagreement([
        _priced("telegram_value_info", "571"),
        _priced("portals_attribute_floor", "2640"),
    ])

    assert gap[0] < scanner.MAX_SOURCE_DISAGREEMENT


def test_single_source_cannot_disagree():
    """Один источник проверить не с чем — и это не повод его винить."""
    assert scanner.source_disagreement([_priced("own", "504")]) is None
    assert scanner.source_disagreement([]) is None


def test_sources_without_a_price_are_skipped():
    """Источник без цены в сравнении не участвует."""
    empty = marketdata.MarketSnapshot(
        collection="Chill Flame", model="Oil Lamp",
        median_price=None, floor_price=None, sample_size=0,
        confidence=Confidence.NONE, velocity_per_day=0.0,
        newest=None, active_listings=0, source="own",
    )

    assert scanner.source_disagreement([empty, _priced("own", "504")]) is None


def test_threshold_leaves_room_for_rare_models():
    """Порог не трогает настоящую премию за редкость.

    Редкая модель к floor коллекции доходит до десятков раз — такие
    находки должны проходить, иначе защита съест то, ради чего всё.
    """
    gap = scanner.source_disagreement([
        _priced("own", "500"),
        _priced("portals_attribute_floor", "15000"),
    ])

    assert gap[0] == Decimal(30)
    assert gap[0] < scanner.MAX_SOURCE_DISAGREEMENT
