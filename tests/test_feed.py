"""Тесты разбора канала находок.

Главное, что проверяется: реальный пост со всеми его переносами строк
разбирается целиком, «Оценка» не приравнивается к «Продано», а
коллекции ранжируются по подтверждённым продажам.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services import feed

# Пост ровно в том виде, в каком он приходит из канала: цена
# отрывается от слова GRAM переносом строки, где-то нет пробела
# после двоеточия, где-то есть лишний пробел перед переносом.
REAL_POST = """#находка_дня

За вчера бот совершил 589 покупок. Какая покупка круче?


😍 SpringBasket-40494 - за 16.43
GRAM (Оценка: 26.00 GRAM)
💪 TamaGadget-18389 - за 102.60 
GRAM (Продано: 215.00 GRAM)
😎 ToyBear-20145 - за 39.76 
GRAM (Оценка:50.00 GRAM)"""


# --- разбор поста ------------------------------------------------------


def test_real_post_parses_completely():
    """Все три находки распознаются, несмотря на переносы строк."""
    finds = feed.parse_post(REAL_POST)

    assert len(finds) == 3
    assert [f.collection for f in finds] == [
        "Spring Basket",
        "Tama Gadget",
        "Toy Bear",
    ]
    assert [f.number for f in finds] == [40494, 18389, 20145]


def test_prices_and_values_exact():
    """Числа берутся как есть, без потери копеек."""
    first = feed.parse_post(REAL_POST)[0]

    assert first.price == Decimal("16.43")
    assert first.value == Decimal("26.00")


def test_sold_is_distinguished_from_estimate():
    """«Продано» — факт, «Оценка» — прикидка; путать их нельзя."""
    finds = feed.parse_post(REAL_POST)

    assert finds[0].realized is False   # Оценка
    assert finds[1].realized is True    # Продано
    assert finds[2].realized is False   # Оценка без пробела после двоеточия


def test_margin_computed():
    """Маржа — во сколько раз оценка выше цены."""
    finds = feed.parse_post(REAL_POST)

    assert finds[1].margin == Decimal("215.00") / Decimal("102.60")
    assert round(float(finds[0].margin), 2) == 1.58


def test_currency_is_ton():
    """GRAM — это переименованный TON, курс 1:1."""
    from app.enums import Currency

    assert feed.parse_post(REAL_POST)[0].currency is Currency.TON


def test_comma_decimal_accepted():
    """Запятая вместо точки — обычное дело в русских постах."""
    finds = feed.parse_post("#находка_дня LolPop-1 - за 10,50 GRAM (Продано: 21,00 GRAM)")

    assert finds[0].price == Decimal("10.50")
    assert finds[0].value == Decimal("21.00")


def test_unwrapped_post_also_parses():
    """Пост без переносов разбирается так же."""
    text = "😍 SpringBasket-40494 - за 16.43 GRAM (Оценка: 26.00 GRAM)"
    assert len(feed.parse_post(text)) == 1


def test_garbage_lines_ignored():
    """Посторонние строки не ломают разбор и не превращаются в находки."""
    text = REAL_POST + "\n\nПодписывайтесь! https://t.me/whatever 100 GRAM"
    assert len(feed.parse_post(text)) == 3


def test_zero_price_rejected():
    """Нулевая цена дала бы бесконечную маржу — такая строка отбрасывается."""
    assert feed.parse_post("Thing-1 - за 0 GRAM (Продано: 5 GRAM)") == []


def test_empty_text_is_empty_result():
    """Пустой пост — пустой список, а не исключение."""
    assert feed.parse_post("") == []
    assert feed.parse_post("просто текст без находок") == []


# --- названия коллекций ------------------------------------------------


def test_camel_case_split():
    """Слитное имя из поста разворачивается в название с пробелами."""
    assert feed.split_camel("SpringBasket") == "Spring Basket"
    assert feed.split_camel("ToyBear") == "Toy Bear"
    assert feed.split_camel("LolPop") == "Lol Pop"


def test_known_collection_wins_over_split():
    """Настоящее название с площадки точнее разбора по заглавным буквам."""
    finds = feed.parse_post(
        "BDayCandle-7 - за 5 GRAM (Продано: 9 GRAM)",
        known={"bdaycandle": "B-Day Candle"},
    )

    assert finds[0].collection == "B-Day Candle"


def test_unknown_collection_falls_back_to_split():
    """Незнакомая коллекция всё равно получает читаемое имя."""
    finds = feed.parse_post(
        "NewThing-7 - за 5 GRAM (Продано: 9 GRAM)", known={"other": "Other"}
    )

    assert finds[0].collection == "New Thing"


# --- ссылка на канал ---------------------------------------------------


def test_web_client_link_parsed():
    """Ссылка из веб-клиента — именно то, что присылает пользователь."""
    ref = feed.parse_channel_ref("https://web.telegram.org/a/#-1002836789307")
    assert ref == 2836789307


def test_username_forms_parsed():
    """@имя и t.me-ссылка дают одно и то же."""
    assert feed.parse_channel_ref("@finds") == "finds"
    assert feed.parse_channel_ref("https://t.me/finds") == "finds"
    assert feed.parse_channel_ref("finds") == "finds"


def test_bot_api_id_stripped():
    """Префикс -100 из Bot API убирается: MTProto ждёт голый id."""
    assert feed.parse_channel_ref("-1002836789307") == 2836789307


def test_empty_channel_rejected():
    """Пустая ссылка — понятная ошибка, а не молчаливый провал."""
    with pytest.raises(ValueError, match="не задан"):
        feed.parse_channel_ref("")


# --- ранжирование коллекций -------------------------------------------


def _add(session, collection, price, value, *, realized, days_ago=0, msg=1, num=1):
    """Записать находку в базу."""
    from app.models import FeedFind, utcnow

    session.add(
        FeedFind(
            message_id=msg,
            posted_at=utcnow() - dt.timedelta(days=days_ago),
            collection=collection,
            number=num,
            price=Decimal(str(price)),
            value=Decimal(str(value)),
            realized=realized,
        )
    )
    session.flush()


def test_realized_sale_outweighs_estimate(session):
    """Подтверждённая продажа весит больше чужой оценки с той же маржой."""
    _add(session, "Sold", 10, 20, realized=True, msg=1)
    _add(session, "Guessed", 10, 20, realized=False, msg=2)

    ranked = feed.rank_collections(session)

    assert ranked[0].collection == "Sold"
    assert ranked[0].score > ranked[1].score


def test_realized_count_reported(session):
    """Видно, сколько находок в коллекции реально продано."""
    _add(session, "Mixed", 10, 20, realized=True, msg=1, num=1)
    _add(session, "Mixed", 10, 15, realized=False, msg=1, num=2)

    score = feed.rank_collections(session)[0]

    assert score.finds == 2
    assert score.realized == 1


def test_old_finds_excluded(session):
    """Находки месячной давности не влияют: рынок с тех пор вычищен."""
    _add(session, "Stale", 10, 30, realized=True, days_ago=40, msg=1)
    _add(session, "Fresh", 10, 12, realized=True, days_ago=1, msg=2)

    names = [s.collection for s in feed.rank_collections(session)]

    assert "Stale" not in names
    assert "Fresh" in names


def test_losing_find_adds_nothing(session):
    """Покупка дороже оценки не повод считать коллекцию удачной."""
    _add(session, "Bad", 20, 10, realized=True, msg=1)

    assert feed.rank_collections(session)[0].score == Decimal(0)


def test_price_range_reported(session):
    """Видно, в каком диапазоне цен работает коллекция."""
    _add(session, "Range", 10, 20, realized=True, msg=1, num=1)
    _add(session, "Range", 100, 200, realized=True, msg=1, num=2)

    score = feed.rank_collections(session)[0]

    assert score.max_price == Decimal("100")
    assert score.median_price in (Decimal("10"), Decimal("100"))


def test_top_collections_limited(session):
    """Сканеру отдаётся ограниченный список, а не всё подряд."""
    for i in range(15):
        _add(session, f"C{i}", 10, 10 + i, realized=True, msg=i + 1)

    assert len(feed.top_collections(session, limit=5)) == 5


def test_empty_feed_gives_nothing(session):
    """Пустой канал — пустой список, а не ошибка."""
    assert feed.top_collections(session) == []
