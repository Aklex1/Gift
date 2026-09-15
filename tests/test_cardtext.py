"""Тесты карточки находки.

Карточка отвечает не на вопрос «что нашлось» — на него отвечает строка
в таблице, — а на вопросы «почему это дёшево» и «где здесь деньги».

Поэтому проверяется в первую очередь не вёрстка, а честность: строка,
под которой нет измерения, не печатается. Пустое место читается как
«неизвестно», а правдоподобное число — как факт, и именно так у нас
однажды появился ROI в 36 000%.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency, Market
from app.services import cardtext


def _card(**over):
    base = dict(
        name="Light Sword #77735",
        market=Market.MRKT,
        external_id="be4d6e1c",
        collection="Light Sword",
        number=77735,
        model="Bifrost",
        backdrop="Seal Brown",
        price_native=Decimal("6.171"),
        currency=Currency.TON,
        price_usd=Decimal("20.52"),
        profit_usd=Decimal("5.42"),
        days_to_sell=4.2,
        rationale={
            "fair_value": "2495",
            "expected_sale_price": "2495",
            "break_even": "1974",
            "net_roi": "0.264",
            "risk_score": 32,
            "sales": {"sales": 36, "source": "fragment"},
            "attributes": [
                {"label": "фон", "name": "Seal Brown", "floor": "2495",
                 "premium": "0.2649", "supply": 25},
                {"label": "модель", "name": "Bifrost", "floor": "2180",
                 "premium": "0.1256", "supply": 140},
            ],
        },
    )
    base.update(over)
    return cardtext.render(**base)


# --- что карточка говорит -----------------------------------------------


def test_card_names_price_forecast_and_zero():
    """Три числа, по которым принимается решение, стоят рядом."""
    text = _card()

    assert "6.171 GRAM · 20.52 $" in text
    assert "Прогноз: 2495 ★" in text
    assert "ноль: 1974 ★" in text


def test_profit_is_shown_in_money():
    """Прибыль названа в деньгах и в процентах сразу.

    «+26.4%» не говорит, стоит ли ради этого шевелиться: от двадцати
    долларов это пять, а от двухсот — пятьдесят.
    """
    text = _card()

    assert "+5.42 $" in text
    assert "+26.4%" in text


def test_attributes_explain_why_it_is_cheap():
    """Запас по признакам — это и есть ответ «почему дёшево»."""
    text = _card()

    assert "фон +26.5%" in text
    assert "модель +12.6%" in text


def test_strongest_attribute_goes_to_the_shelf_line():
    """На полке — самый сильный признак, с floor'ом и тиражом."""
    text = _card()

    assert "фон «Seal Brown» — floor 2495 ★, выпущено 25" in text


def test_double_margin_when_two_attributes_hold_the_price():
    """Два признака выше цены — это двойной запас."""
    assert "Двойной запас" in _card()


def test_one_attribute_is_not_a_double_margin():
    """Один признак двойным запасом не объявляется."""
    text = _card(rationale={
        "attributes": [
            {"label": "фон", "name": "Seal Brown", "floor": "2495",
             "premium": "0.26"},
            {"label": "модель", "name": "Bifrost", "floor": "100",
             "premium": "-0.40"},
        ],
    })

    assert "Двойной запас" not in text


def test_link_points_at_the_gift_itself():
    """Ссылка на подарок складывается из коллекции и номера."""
    assert "https://t.me/nft/LightSword-77735" in _card()


def test_venue_link_added_where_known():
    """Для MRKT добавляется ссылка в мини-приложение площадки."""
    assert "https://t.me/mrkt/app?startapp=be4d6e1c" in _card()


def test_no_venue_link_is_not_invented():
    """Для площадки без известной ссылки выдумывать её не нужно."""
    text = _card(market=Market.PORTALS)

    assert text.count("🔗") == 1


# --- чего карточка не говорит -------------------------------------------


def test_unknown_profit_is_not_zero():
    """Без курса прибыль неизвестна, а не равна нулю."""
    text = _card(profit_usd=None, price_usd=None)

    assert "0.00 $" not in text
    assert "Прибыль: —" in text


def test_missing_attributes_drop_the_lines():
    """Нет данных по признакам — нет строк про признаки.

    Пустая «Полка:» выглядела бы как измеренное отсутствие запаса.
    """
    text = _card(rationale={"net_roi": "0.10"})

    assert "Полка" not in text
    assert "🎯" not in text


def test_no_sales_no_sales_line():
    """О скорости продаж молчим, пока сделок не видели."""
    text = _card(days_to_sell=None, rationale={"net_roi": "0.10"})

    assert "⏱" not in text


def test_broken_numbers_do_not_break_the_card():
    """Испорченное значение становится прочерком, а не исключением."""
    text = _card(rationale={"fair_value": "не число", "net_roi": "0.1"})

    assert "Прогноз: — ★" in text


def test_gift_without_a_number_has_no_gift_link():
    """Без номера адрес подарка не собрать — и подделывать его нечем."""
    text = _card(number=None, market=Market.PORTALS)

    assert "t.me/nft" not in text


# --- одни строки на бота и на панель ------------------------------------


def test_panel_and_bot_show_the_same_lines():
    """Панель и бот берут строки из одного места.

    Две копии одной логики расходятся — это уже случилось с проверкой
    «сканер молчит», которая была написана дважды, и одна из копий
    осталась старой. Здесь расхождение ловится сразу.
    """
    common = dict(
        name="Light Sword #1", market=Market.MRKT, external_id="x",
        collection="Light Sword", number=1, model="Bifrost", backdrop=None,
        price_native=Decimal("6.17"), currency=Currency.TON,
        price_usd=Decimal("20"), profit_usd=Decimal("5"),
        rationale={"net_roi": "0.26", "fair_value": "2495"},
    )

    rows = cardtext.lines(**common)
    text = cardtext.render(**common)

    for _icon, line in rows:
        assert line in text


def test_lines_carry_no_markup():
    """Строки отдаются без разметки: её добавляет тот, кто показывает."""
    rows = cardtext.lines(
        name="Light Sword #1", market=Market.MRKT, external_id="x",
        collection="Light Sword", number=1, model="Bifrost", backdrop=None,
        price_native=Decimal("6.17"), currency=Currency.TON,
        price_usd=None, profit_usd=None, rationale={"net_roi": "0.26"},
    )

    assert all("<" not in line for _icon, line in rows)
