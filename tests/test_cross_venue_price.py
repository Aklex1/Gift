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


# --- floor признака, а не только модели --------------------------------
#
# Живой экран: Mousse Cake #16119 на Portals.
#
#   Model     Crypto Chips  0.5%   18.4 GRAM
#   Symbol    Butterfly     0.6%    5.51 GRAM
#   Backdrop  Black         1%     21.24 GRAM
#   Мин. цена коллекции             5 GRAM
#
# «Мин. цена» — floor всей коллекции, самый дешёвый Mousse Cake любого
# вида. К этому подарку она отношения не имеет: у него три редких
# признака сразу.


FLOORS = {
    "models": {"Crypto Chips": "18.4", "Glacier": "5"},
    "symbols": {"Butterfly": "5.51", "Top Hat": "5"},
    "backdrops": {"Black": "21.24", "Neon Blue": "3.95"},
}


def _gift(model=None, backdrop=None, symbol=None):
    from app.adapters.base import GiftRef

    return GiftRef(collection="Mousse Cake", number=16119,
                   model=model, backdrop=backdrop, symbol=symbol)


def test_rarest_trait_sets_the_price():
    """Подарок стоит не меньше самого дорогого из своих признаков.

    Иначе покупателю пришлось бы взять самый дешёвый лот с этим
    признаком — а он и стоит floor.
    """
    floor, why = scanner.rarest_attribute_floor(
        FLOORS, _gift("Crypto Chips", "Black", "Butterfly")
    )

    assert floor == Decimal("21.24")
    assert "фон" in why and "Black" in why


def test_rare_backdrop_on_a_common_model_is_not_missed():
    """Рядовая модель с редким фоном больше не оценивается по модели.

    Раньше брался только floor модели: такой подарок выглядел дорогим
    и отбрасывался, хотя один его фон стоил вчетверо больше.
    """
    floor, why = scanner.rarest_attribute_floor(FLOORS, _gift("Glacier", "Black"))

    assert floor == Decimal("21.24")
    assert "фон" in why


def test_unknown_traits_are_skipped():
    """Признак, которого нет в таблице площадки, просто пропускается."""
    floor, _ = scanner.rarest_attribute_floor(
        FLOORS, _gift("Неизвестная", "Black")
    )

    assert floor == Decimal("21.24")


def test_gift_without_known_traits_has_no_floor():
    """Без единого известного признака floor взять неоткуда.

    Подставлять сюда цену коллекции нельзя: она про рядовой экземпляр,
    а не про этот.
    """
    floor, why = scanner.rarest_attribute_floor(FLOORS, _gift("Нет", "Нет", "Нет"))

    assert floor is None and why is None


def test_collection_floor_is_not_used_as_a_trait():
    """Floor коллекции в расчёт признаков не попадает.

    Он равен цене самого дешёвого экземпляра любого вида — для
    подарка с редкими признаками это занижение в разы.
    """
    floor, _ = scanner.rarest_attribute_floor(FLOORS, _gift(symbol="Butterfly"))

    assert floor == Decimal("5.51")


# --- наценка не поднимает цену выше floor ------------------------------
#
# Третий живой случай, и самый тихий из трёх. Snake Box #108974 на
# Portals: модель, символ, фон и минимальная цена коллекции — всё по
# 3.95 GRAM, и сам лот стоит столько же. То есть он и есть floor.
#
# Панель показывала по нему разрыв 0% и ROI 9.2%. Вся «прибыль»
# бралась из наценки стратегии +15%: потолок площадки применялся до
# неё, и наценка его перешагивала. Продать выше floor нельзя — рядом
# стоят такие же лоты, и покупатель возьмёт их раньше.


def test_buying_at_floor_is_not_a_deal(session, fees):
    """Покупка ровно по floor прибыли не даёт, сколько ни наценивай."""
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )

    result = valuation.evaluate(
        session,
        buy_market=Market.PORTALS, buy_price=Decimal("394"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, target_markup=Decimal("0.15"),
        venue_floor=Decimal("394"),
    )

    assert result.expected_sale_price == Decimal("394")
    assert result.net_roi < 0
    assert any("не продать" in reason for reason in result.reasons)


def test_markup_does_not_invent_profit(session, fees):
    """Без наценки и с наценкой у лота по floor итог одинаковый.

    Наценка — пожелание продавца, а не цена рынка. Если она меняет
    ROI лота, купленного по floor, значит прибыль взялась из неё.
    """
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )
    common = dict(
        buy_market=Market.PORTALS, buy_price=Decimal("394"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, venue_floor=Decimal("394"),
    )

    without = valuation.evaluate(session, **common, target_markup=Decimal("0"))
    with_markup = valuation.evaluate(session, **common, target_markup=Decimal("0.5"))

    assert without.net_roi == with_markup.net_roi


def test_real_discount_still_earns(session, fees):
    """А настоящая находка по-прежнему считается прибыльной.

    Потолок обрезает пожелания, а не саму сделку: купили заметно ниже
    floor — продаёте по floor и зарабатываете разницу.
    """
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )

    result = valuation.evaluate(
        session,
        buy_market=Market.PORTALS, buy_price=Decimal("300"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, target_markup=Decimal("0.15"),
        venue_floor=Decimal("394"),
    )

    assert result.expected_sale_price == Decimal("394")
    assert result.net_roi > Decimal("0.2")


# --- цена безубыточности ------------------------------------------------
#
# Идея подсмотрена у чужого бота: рядом с ценой покупки он показывает
# «ноль» — цену, ниже которой продажа уходит в минус. Это делает строку
# самопроверяемой: если ноль выше floor площадки, продать без убытка
# нельзя, и никакая оценка этого не отменяет.


def test_break_even_is_above_the_cost(session, fees):
    """Ноль выше затрат ровно на комиссию продажи."""
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )

    result = valuation.evaluate(
        session,
        buy_market=Market.PORTALS, buy_price=Decimal("394"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, venue_floor=Decimal("394"),
    )

    assert result.break_even > Decimal("394")


def test_break_even_above_the_floor_means_no_deal(session, fees):
    """Ноль выше потолка площадки — верный признак убытка.

    Ровно случай Snake Box: лот стоял по floor, и чтобы выйти в ноль,
    продать его пришлось бы дороже floor. То есть никак.
    """
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )

    result = valuation.evaluate(
        session,
        buy_market=Market.PORTALS, buy_price=Decimal("394"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, target_markup=Decimal("0.15"),
        venue_floor=Decimal("394"),
    )

    assert result.break_even > result.expected_sale_price
    assert result.net_roi < 0


def test_real_find_clears_its_break_even(session, fees):
    """У настоящей находки ноль ниже цены продажи."""
    snapshot = marketdata.snapshot_from_attribute_floor(
        collection="Snake Box", model="Pink Bloom",
        model_floor=Decimal("394"), listed_count=2, models_listed=40,
    )

    result = valuation.evaluate(
        session,
        buy_market=Market.PORTALS, buy_price=Decimal("300"),
        sell_market=Market.PORTALS, snapshot=snapshot,
        is_official_api=False, venue_floor=Decimal("394"),
    )

    assert result.break_even < result.expected_sale_price
    assert result.net_roi > 0
