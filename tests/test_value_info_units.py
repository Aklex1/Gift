"""Тесты единиц в официальной оценке Telegram.

Telegram отдаёт цены «в наименьших единицах валюты, указанной в
currency», но какая это единица для GRAM, в документации не сказано.
Раньше GRAM стоял в исключениях вместе со звёздами и не делился ни на
что — цены по подаркам, чей резейл считается в GRAM, выходили в сто раз
больше настоящих.

Живой случай: Chill Flame за 504 звезды, floor из оценки 198 800,
ROI 36 188%. После деления floor становится 4.97 GRAM — ровно столько
эта коллекция и стоит на Fragment (5) и Portals (4.15).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency


class _Res:
    """Ответ payments.getUniqueStarGiftValueInfo."""

    def __init__(self, currency, floor, average=None):
        self.currency = currency
        self.floor_price = floor
        self.average_price = average
        self.value = floor
        self.last_sale_price = None
        self.last_sale_date = None
        self.listed_count = 33680
        self.value_is_average = False
        self.initial_sale_price = None


async def _info(res):
    """Разобранная оценка для этого ответа."""
    from app.adapters.telegram_mtproto import TelegramAdapter

    class _Gateway:
        async def call(self, _request):
            return res

    adapter = TelegramAdapter.__new__(TelegramAdapter)
    # Шлюз — свойство с ленивым созданием; подменяем поле под ним,
    # чтобы не поднимать настоящую сессию Telegram ради разбора чисел.
    adapter._gateway = _Gateway()
    return await TelegramAdapter.value_info(adapter, "chillflame-114734")


@pytest.mark.asyncio
async def test_gram_amounts_are_hundredths():
    """Цена в GRAM приходит сотыми долями — её нужно делить.

    Числа из живого случая: 19 880 000 сотых это 198 800 без деления
    и 4.97 GRAM с делением. Вторая величина совпадает с рынком.
    """
    info = await _info(_Res("TON", 497, 1059))

    assert info["currency"] is Currency.TON
    assert info["floor_price"] == Decimal("4.97")
    assert info["average_price"] == Decimal("10.59")


@pytest.mark.asyncio
async def test_stars_are_whole():
    """А звезда сама себе наименьшая единица — её делить не на что.

    Проверяется на числах второго живого случая: Candy Cane отдавал
    floor 571 и медиану 840, и они верны как есть.
    """
    info = await _info(_Res("XTR", 571, 840))

    assert info["currency"] is Currency.STARS
    assert info["floor_price"] == Decimal("571")
    assert info["average_price"] == Decimal("840")


@pytest.mark.asyncio
async def test_fiat_is_hundredths_too():
    """Фиат делится так же — это общее правило, а не исключение."""
    info = await _info(_Res("USD", 1999))

    assert info["floor_price"] == Decimal("19.99")


@pytest.mark.asyncio
async def test_missing_values_stay_missing():
    """Пустое поле остаётся пустым, а не превращается в ноль.

    Ноль в цене — утверждение, что подарок ничего не стоит; отсутствие
    данных таким утверждением не является.
    """
    info = await _info(_Res("TON", None))

    assert info["floor_price"] is None
    assert info["average_price"] is None
