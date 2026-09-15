"""Тесты источника Fragment.

Fragment отвечает на вопрос, на который не отвечает больше никто:
почём подарок реально **купили** и когда. Отсюда берётся скорость
продаж, без которой каждая сделка получает надбавку к риску за
незнание.

Цена этого — разбор чужой разметки. Поэтому проверяется в первую
очередь не «сколько нашли», а «что происходит, когда разметка не
та»: неполная запись должна пропадать, а не доезжать до расчёта
половиной значений.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.adapters.fragment import (
    FilterIgnored,
    FragmentAdapter,
    attribute_applied,
    parse_cards,
    short_collection_name,
)
from app.enums import Capability, CapabilityStatus, Currency, Market


def _card(
    slug: str = "chillflame-28407",
    name: str = "Chill Flame",
    price: str = "5",
    icon: str = "ton",
    status: str = "For sale",
    moment: str | None = "2026-09-15T05:39:27+00:00",
) -> str:
    """Карточка каталога в том виде, в каком её отдаёт Fragment."""
    time_tag = f'<time datetime="{moment}" class="short">когда-то</time>' if moment else ""
    return (
        f'<a href="/gift/{slug}?filter=sold" class="tm-grid-item">'
        f'<div class="tm-grid-item-content">'
        f'<div class="tm-grid-item-name wide-only">'
        f'<span class="item-name">{name}</span>'
        f'<span class="item-num">&nbsp;#1</span></div>'
        f'<div class="tm-grid-item-desc wide-only">{time_tag}</div>'
        f'<div class="tm-grid-item-values">'
        f'<div class="tm-grid-item-value tm-value icon-before icon-{icon}">{price}</div>'
        f'<div class="tm-grid-item-status tm-status-unavail">{status}</div>'
        f"</div></div></a>"
    )


def _page(*cards: str, applied: str = "") -> str:
    """Страница каталога: карточки плюс эхо применённого фильтра."""
    return f'<html><body>{applied}{"".join(cards)}</body></html>'


# --- разбор карточек ---------------------------------------------------


def test_reads_price_time_and_name():
    """Из карточки достаются цена, валюта, время и имя коллекции."""
    cards = parse_cards(_page(_card()))

    assert len(cards) == 1
    card = cards[0]
    assert card.slug == "chillflame-28407"
    assert card.collection == "Chill Flame"
    assert card.number == 28407
    assert card.price == Decimal("5")
    assert card.currency is Currency.TON
    assert card.moment == dt.datetime(2026, 9, 15, 5, 39, 27)


def test_fractional_price():
    """Дробные цены — обычное дело, целочисленный разбор их терял бы."""
    assert parse_cards(_page(_card(price="3.25")))[0].price == Decimal("3.25")


def test_unknown_currency_card_dropped():
    """Цена в незнакомой валюте пропускается, а не берётся как есть.

    Взять число без валюты — то же, что приравнять 5 звёзд к 5 TON.
    """
    assert parse_cards(_page(_card(icon="star"))) == []


def test_card_without_price_dropped():
    """Карточка без цены не годится ни для floor, ни для истории."""
    broken = _card().replace(
        '<div class="tm-grid-item-value tm-value icon-before icon-ton">5</div>', ""
    )
    assert parse_cards(_page(broken)) == []


def test_unrelated_markup_yields_nothing():
    """Смена вёрстки даёт пусто, а не мусор."""
    assert parse_cards("<html><body><p>Ничего похожего</p></body></html>") == []


def test_several_cards_kept_in_order():
    """Порядок карточек сохраняется: он и есть сортировка площадки."""
    page = _page(
        _card(slug="chillflame-1", price="4"),
        _card(slug="chillflame-2", price="5"),
        _card(slug="chillflame-3", price="6"),
    )
    assert [c.price for c in parse_cards(page)] == [
        Decimal("4"), Decimal("5"), Decimal("6")
    ]


def test_card_price_does_not_leak_from_neighbour():
    """Цена соседней карточки не приписывается той, у которой её нет."""
    broken = _card(slug="chillflame-1").replace(
        '<div class="tm-grid-item-value tm-value icon-before icon-ton">5</div>', ""
    )
    cards = parse_cards(_page(broken, _card(slug="chillflame-2", price="77")))

    assert [(c.slug, c.price) for c in cards] == [("chillflame-2", Decimal("77"))]


# --- короткое имя коллекции -------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        ("Chill Flame", "chillflame"),
        ("Plush Pepe", "plushpepe"),
        ("B-Day Candle", "bdaycandle"),
        ("", ""),
    ],
)
def test_short_names(given, expected):
    """Fragment ждёт то же короткое имя, что и Portals."""
    assert short_collection_name(given) == expected


# --- проверка, что фильтр применён ------------------------------------


def test_applied_filter_is_recognised():
    """Принятый фильтр площадка повторяет в ссылках страницы."""
    page = _page(applied='<a href="/gifts/chillflame?attr%5BModel%5D=%5B%22Lego%22%5D">')

    assert attribute_applied(page, "Model", "Lego")


def test_filter_with_space_recognised():
    """Значения с пробелом кодируются, но узнаются."""
    page = _page(
        applied='<a href="/gifts/x?attr%5BBackdrop%5D=%5B%22Marine%20Blue%22%5D">'
    )

    assert attribute_applied(page, "Backdrop", "Marine Blue")


def test_ignored_filter_is_detected():
    """Отброшенный фильтр не упоминается — это и есть признак."""
    assert not attribute_applied(_page(_card()), "Model", "Lego")


# --- поведение адаптера ------------------------------------------------


class _Adapter(FragmentAdapter):
    """Адаптер с подставленной страницей вместо сети."""

    def __init__(self, page: str) -> None:
        super().__init__(base_url="https://fragment.test")
        self.page = page
        self.asked: list[dict] = []

    async def request_text(self, method, path, **kwargs):
        self.asked.append({"path": path, **kwargs.get("params", {})})
        return self.page


@pytest.mark.asyncio
async def test_history_returns_sales_with_time():
    """История — это цена сделки и её время."""
    page = _page(
        _card(slug="chillflame-1", price="4", moment="2026-09-15T05:39:27+00:00"),
        applied='attr%5BModel%5D=%5B%22Lego%22%5D',
    )
    adapter = _Adapter(page)

    sales = await adapter.history(collection="Chill Flame", model="Lego")

    assert len(sales) == 1
    assert sales[0].price == Decimal("4")
    assert sales[0].happened_at == dt.datetime(2026, 9, 15, 5, 39, 27)
    assert sales[0].market is Market.FRAGMENT
    assert sales[0].gift.model == "Lego"


@pytest.mark.asyncio
async def test_sale_without_time_is_not_history():
    """Продажа без даты для скорости бесполезна — она отбрасывается."""
    adapter = _Adapter(_page(_card(moment=None)))

    assert await adapter.history(collection="Chill Flame") == []


@pytest.mark.asyncio
async def test_each_sale_is_a_separate_fact():
    """Один подарок продают не раз — ключ сделки включает время.

    Иначе вторая продажа того же экземпляра схлопнулась бы с первой
    как дубликат, и история осталась бы неполной.
    """
    page = _page(
        _card(slug="chillflame-1", moment="2026-09-15T05:39:27+00:00"),
        _card(slug="chillflame-1", moment="2026-07-01T10:00:00+00:00"),
    )
    adapter = _Adapter(page)

    sales = await adapter.history(collection="Chill Flame")

    assert len({sale.external_id for sale in sales}) == 2


@pytest.mark.asyncio
async def test_history_asks_for_recent_sales():
    """За историей идём к проданным, отсортированным по свежести."""
    adapter = _Adapter(_page(_card()))

    await adapter.history(collection="Chill Flame")

    assert adapter.asked[0]["path"] == "/gifts/chillflame"
    assert adapter.asked[0]["filter"] == "sold"
    assert adapter.asked[0]["sort"] == "ending"


@pytest.mark.asyncio
async def test_search_asks_for_cheapest_first():
    """За floor идём к выставленным, отсортированным по цене."""
    adapter = _Adapter(_page(_card(status="For sale")))

    await adapter.search(collection="Chill Flame")

    assert adapter.asked[0]["filter"] == "sale"
    assert adapter.asked[0]["sort"] == "price_asc"


@pytest.mark.asyncio
async def test_model_filter_is_sent_as_a_list():
    """Атрибут передаётся списком — такой формат ждёт площадка."""
    adapter = _Adapter(_page(_card(), applied='attr%5BModel%5D=%5B%22Lego%22%5D'))

    await adapter.search(collection="Chill Flame", model="Lego")

    assert adapter.asked[0]["attr[Model]"] == '["Lego"]'


@pytest.mark.asyncio
async def test_ignored_model_filter_refuses_the_answer():
    """Незнакомую модель Fragment молча заменяет всей коллекцией.

    Принять такой ответ за срез по редкой модели значило бы подставить
    в её оценку цену рядового экземпляра — заниженную в разы.
    """
    adapter = _Adapter(_page(_card()))

    with pytest.raises(FilterIgnored):
        await adapter.search(collection="Chill Flame", model="Лего")


@pytest.mark.asyncio
async def test_max_price_stops_at_the_first_dearer_lot():
    """Список отсортирован по возрастанию — дальше искать нечего."""
    page = _page(
        _card(slug="chillflame-1", price="4"),
        _card(slug="chillflame-2", price="9"),
        _card(slug="chillflame-3", price="4"),
    )
    adapter = _Adapter(page)

    rows = await adapter.search(collection="Chill Flame", max_price=Decimal("5"))

    assert [row.external_id for row in rows] == ["chillflame-1"]


# --- границы возможностей ----------------------------------------------


def test_trading_is_closed():
    """На Fragment бот не торгует: сделка требует ключа от кошелька."""
    adapter = FragmentAdapter()

    for capability in (
        Capability.BUY, Capability.LIST, Capability.REPRICE,
        Capability.CANCEL, Capability.TRANSFER,
    ):
        assert adapter.status_of(capability) is CapabilityStatus.UNAVAILABLE


def test_not_offered_as_a_place_to_search():
    """Площадку только для чтения стратегии не предлагают.

    Иначе в кандидатах оказались бы лоты, которые нельзя купить ни при
    каких настройках.
    """
    from app.adapters.registry import tradable_markets

    assert Market.FRAGMENT not in tradable_markets()
    assert Market.TELEGRAM in tradable_markets()


def test_read_only_flag_does_not_depend_on_tokens():
    """Признак «только чтение» — свойство площадки, а не настроек.

    Статус BUY гаснет и от протухшего токена; если бы список строился
    по нему, площадки то исчезали бы из настроек стратегии, то
    возвращались.
    """
    adapter = FragmentAdapter()
    adapter.capabilities[Capability.BUY] = CapabilityStatus.EXPERIMENTAL

    assert adapter.read_only


# --- обход по кругу ----------------------------------------------------


def _gift(session, collection, model, number=1):
    """Подарок с активным лотом — то, что бот сейчас видит."""
    from app.models import Gift, Listing, utcnow

    gift = Gift(
        canonical_key=f"{collection}#{number}",
        collection=collection, model=model, number=number,
    )
    session.add(gift)
    session.flush()
    session.add(
        Listing(
            market=Market.PORTALS, external_id=f"p-{number}", gift_id=gift.id,
            price=Decimal("5"), currency=Currency.TON, price_stars=Decimal("325"),
            is_active=True, seen_at=utcnow(),
        )
    )
    session.flush()
    return gift


def test_pairs_cover_model_and_collection(session):
    """По каждому встреченному подарку смотрим и модель, и коллекцию.

    Модель отвечает на вопрос о цене, коллекция — о скорости: по ней
    сделок в разы больше, и выборка получается представительнее.
    """
    from app.services import fragment_sync

    _gift(session, "Chill Flame", "Lego")

    pairs = fragment_sync.pairs_of_interest(session)

    assert ("Chill Flame", "Lego") in pairs
    assert ("Chill Flame", None) in pairs


def test_pairs_ignore_stale_listings(session):
    """Лот, которого давно не видно, обходить незачем."""
    import datetime as dt

    from app.models import Listing, utcnow
    from app.services import fragment_sync

    _gift(session, "Chill Flame", "Lego")
    listing = session.query(Listing).one()
    listing.seen_at = utcnow() - dt.timedelta(days=30)
    listing.is_active = True
    session.flush()

    assert fragment_sync.pairs_of_interest(session) == []


def test_pairs_have_no_duplicates(session):
    """Две карточки одной модели — одна пара, а не два запроса."""
    from app.services import fragment_sync

    _gift(session, "Chill Flame", "Lego", number=1)
    _gift(session, "Chill Flame", "Lego", number=2)

    pairs = fragment_sync.pairs_of_interest(session)

    assert len(pairs) == len(set(pairs)) == 2


def test_due_prefers_the_longest_untouched():
    """Первыми обновляем то, что дольше всех не трогали."""
    import datetime as dt

    from app.models import utcnow
    from app.services import fragment_sync

    now = utcnow()
    state = {
        "A|": (now - dt.timedelta(days=2)).isoformat(),
        "B|": (now - dt.timedelta(days=5)).isoformat(),
    }
    pairs = [("A", None), ("B", None), ("C", None)]

    order = fragment_sync.due(pairs, state)

    # C не обходили ни разу — он первый, дальше по давности.
    assert order == [("C", None), ("B", None), ("A", None)]


def test_due_skips_fresh_pairs():
    """Свежую пару второй раз за проход не трогаем."""
    from app.models import utcnow
    from app.services import fragment_sync

    state = {"A|Lego": utcnow().isoformat()}

    assert fragment_sync.due([("A", "Lego")], state) == []


def test_due_respects_the_cap():
    """За проход уходит не больше потолка запросов."""
    from app.services import fragment_sync

    pairs = [(f"C{i}", None) for i in range(fragment_sync.MAX_PAIRS + 15)]

    assert len(fragment_sync.due(pairs, {})) == fragment_sync.MAX_PAIRS


def test_broken_state_does_not_break_the_round():
    """Испорченная отметка времени — повод обойти пару, а не упасть."""
    from app.services import fragment_sync

    assert fragment_sync.due([("A", None)], {"A|": "не дата"}) == [("A", None)]
