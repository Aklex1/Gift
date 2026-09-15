"""Тесты валюты и единиц в официальной оценке Telegram.

Telegram показывает цены подарков в валюте аккаунта: у одного это
звёзды, у другого рубли. Живой ответ с сервера:

    Валюта ответа: RUB
    floor_price    46800  ->  468.00 ₽
    initial_sale_price 23200 при initial_sale_stars 176

Второе — проверка первого: 232 ₽ за 176 звёзд это 1.32 ₽ за звезду,
ровно её цена. Значит фиат приходит сотыми долями, и это факт, а не
предположение.

Раньше всё, кроме звёзд, считалось GRAM. Рублёвый floor 468 ₽
превращался в 468 GRAM, то есть в 46 700 звёзд вместо 399 — отсюда и
брались ROI в десятки тысяч процентов у лота, стоящего ровно по рынку.
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


@pytest.fixture(autouse=True)
def fresh_cache():
    """Каждому тесту — чистый кэш оценок.

    Оценки кэшируются по slug на весь процесс, иначе проход тратил бы
    по запросу на лот. В тестах подарок один и тот же, и без сброса
    второй тест читал бы ответ первого.
    """
    from app.adapters import telegram_mtproto as tm

    tm.forget_value_cache()
    yield
    tm.forget_value_cache()


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
async def test_roubles_are_kopecks():
    """Рублёвый ответ приходит копейками и остаётся рублями.

    Числа живого ответа: 46 800 копеек это 468 ₽. По курсу того же дня
    это 399 звёзд — ровно столько стоят 4 GRAM, за которые та же
    коллекция продаётся на Fragment.
    """
    info = await _info(_Res("RUB", 46800, 105900))

    assert info["currency"] is Currency.RUB
    assert info["floor_price"] == Decimal("468")
    assert info["average_price"] == Decimal("1059")


@pytest.mark.asyncio
async def test_star_price_confirms_the_scale():
    """Цена звезды из самого ответа подтверждает масштаб.

    initial_sale_price и initial_sale_stars — одна и та же сумма в
    двух валютах, так что делитель проверяется по ним, а не по вере.
    """
    info = await _info(_Res("RUB", 46800))
    info["raw"]["initial_sale_price"] = 23200
    info["raw"]["initial_sale_stars"] = 176

    per_star = (
        Decimal(info["raw"]["initial_sale_price"]) / info["divisor"]
        / Decimal(info["raw"]["initial_sale_stars"])
    )

    assert Decimal("1") < per_star < Decimal("2")


@pytest.mark.asyncio
async def test_unknown_currency_is_not_guessed():
    """Незнакомый код валюты не подменяется ни звёздами, ни GRAM.

    Число без валюты — не цена. Раньше сюда подставлялся GRAM, и
    рублёвый floor становился оценкой в сто раз выше рыночной.
    """
    info = await _info(_Res("EUR", 46800))

    assert info["currency"] is None


@pytest.mark.asyncio
async def test_gram_is_nanotons():
    """Для GRAM берётся наименьшая единица сети — нанотон.

    Живого ответа в GRAM мы ещё не видели, поэтому масштаб не
    проверен. Если он окажется иным, лот отсеется как расхождение
    источников — превратиться в находку века он не сможет.
    """
    info = await _info(_Res("TON", 4_690_000_000))

    assert info["currency"] is Currency.TON
    assert info["floor_price"] == Decimal("4.69")


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
async def test_dollars_are_cents():
    """Доллар делится так же, как рубль: это общее правило для фиата."""
    info = await _info(_Res("USD", 1999))

    assert info["currency"] is Currency.USD
    assert info["floor_price"] == Decimal("19.99")


@pytest.mark.asyncio
async def test_missing_values_stay_missing():
    """Пустое поле остаётся пустым, а не превращается в ноль.

    Ноль в цене — утверждение, что подарок ничего не стоит; отсутствие
    данных таким утверждением не является.
    """
    info = await _info(_Res("RUB", None))

    assert info["floor_price"] is None
    assert info["average_price"] is None
