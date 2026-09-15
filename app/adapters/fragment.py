"""Адаптер Fragment — официальной витрины Telegram на TON.

Fragment закрывает дыру, которую не закрывает ничто другое: он
показывает **состоявшиеся продажи** с ценой и временем. MTProto
глобальной истории чужих сделок не отдаёт, приватные API площадок —
тоже, и до сих пор скорость продаж бот знал только по собственным
наблюдениям. Из-за этого почти каждая сделка получала надбавку к риску
«скорость продаж неизвестна», и отбор был строже, чем данные требуют.

Чем здесь торгуют, тем здесь и не торгуем. Сделка на Fragment — это
транзакция, подписанная кошельком; бот приватных ключей не хранит и
хранить не должен. Поэтому все write-возможности закрыты наглухо, а не
спрятаны за флагом конфига.

Публичного API нет — страницы разбираются как разметка. Это хрупко по
своей природе, поэтому разбор устроен так, чтобы при любой правке
вёрстки отдавать пусто, а не мусор: цена без валюты, запись без
ссылки, страница без карточек — всё это просто пропускается.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal, InvalidOperation

from app.adapters.base import (
    Capability,
    CapabilityStatus,
    GiftRef,
    ListingDTO,
    SaleDTO,
)
from app.adapters.http_base import HttpMarketAdapter
from app.config import settings
from app.enums import Currency, Market

log = logging.getLogger(__name__)

#: Карточка лота в каталоге.
ITEM_SPLIT = re.compile(r'<a\s+href="/gift/')
#: Ссылка вида chillflame-28407.
SLUG_RE = re.compile(r"^([a-z0-9]+)-(\d+)\?")
#: Цена вместе с валютой: класс иконки — единственное указание на неё.
PRICE_RE = re.compile(r'tm-grid-item-value[^"]*icon-([a-z]+)">\s*([\d.,]+)\s*<')
#: Время: для проданного — момент сделки, для выставленного — срок лота.
TIME_RE = re.compile(r'<time datetime="([^"]+)"')
#: Состояние карточки: For sale / Sold / Unavailable.
STATUS_RE = re.compile(r'tm-grid-item-status[^"]*">\s*([^<]+?)\s*<')
#: Человекочитаемое имя коллекции.
NAME_RE = re.compile(r'<span class="item-name">\s*([^<]+?)\s*</span>')

#: Иконка валюты -> валюта. GRAM — то же, что TON: переименование
#: 2026 года не поменяло ни класс иконки, ни номинал.
ICON_CURRENCY = {"ton": Currency.TON, "gram": Currency.TON}

#: Сколько карточек отдаёт одна страница каталога.
PAGE_SIZE = 60

#: Поля атрибутов в том порядке, в каком их показывает карточка.
ATTRIBUTES = ("Model", "Backdrop", "Symbol")


class FilterIgnored(Exception):
    """Площадка не приняла фильтр по атрибуту.

    Fragment на незнакомое значение не отвечает пустым списком — он
    молча отдаёт всю коллекцию. Принять такой ответ за срез по модели
    значило бы подставить в оценку редкой модели цену рядового
    экземпляра, то есть занизить её в разы.
    """


def short_collection_name(collection: str) -> str:
    """Короткое имя коллекции в том виде, в каком его ждёт Fragment.

    Ровно тот же формат, что у Portals: "Chill Flame" -> "chillflame".
    """
    return re.sub(r"[^a-z0-9]", "", (collection or "").lower())


def attribute_applied(html: str, field: str, value: str) -> bool:
    """Приняла ли страница фильтр по атрибуту.

    Принятый фильтр Fragment переносит во все ссылки страницы,
    отброшенный — не упоминает вовсе. Это единственный способ отличить
    срез по модели от всей коллекции: количество карточек в обоих
    случаях выглядит одинаково правдоподобно.
    """
    from urllib.parse import quote

    marker = f"attr%5B{field}%5D=" + quote(json.dumps([value]), safe="")
    return marker in html


def _decimal(raw: str) -> Decimal | None:
    """Цена из разметки: разделители тысяч встречаются, мусор — тоже."""
    try:
        return Decimal(raw.replace(",", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None


def _moment(raw: str) -> dt.datetime | None:
    """Время карточки в наивном UTC — в таком виде его хранит БД."""
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


class FragmentCard:
    """Разобранная карточка каталога."""

    def __init__(
        self,
        *,
        slug: str,
        collection: str,
        number: int | None,
        price: Decimal,
        currency: Currency,
        status: str,
        moment: dt.datetime | None,
    ) -> None:
        self.slug = slug
        self.collection = collection
        self.number = number
        self.price = price
        self.currency = currency
        self.status = status
        self.moment = moment


def parse_cards(html: str) -> list[FragmentCard]:
    """Разобрать страницу каталога в список карточек.

    Карточка без ссылки, без цены или с незнакомой валютой
    пропускается: неполная запись хуже её отсутствия — она пойдёт в
    расчёт наравне с полными.
    """
    cards: list[FragmentCard] = []
    for chunk in ITEM_SPLIT.split(html)[1:]:
        # Карточка кончается там, где начинается следующая; split уже
        # это сделал, остаётся отрезать хвост страницы.
        chunk = chunk[:4000]

        slug_match = SLUG_RE.match(chunk)
        if slug_match is None:
            continue
        short, number = slug_match.group(1), slug_match.group(2)

        price_match = PRICE_RE.search(chunk)
        if price_match is None:
            continue
        currency = ICON_CURRENCY.get(price_match.group(1))
        price = _decimal(price_match.group(2))
        if currency is None or price is None or price <= 0:
            continue

        name_match = NAME_RE.search(chunk)
        status_match = STATUS_RE.search(chunk)
        time_match = TIME_RE.search(chunk)

        cards.append(
            FragmentCard(
                slug=f"{short}-{number}",
                collection=(name_match.group(1) if name_match else short),
                number=int(number),
                price=price,
                currency=currency,
                status=(status_match.group(1) if status_match else ""),
                moment=_moment(time_match.group(1)) if time_match else None,
            )
        )
    return cards


class FragmentAdapter(HttpMarketAdapter):
    """Fragment: чтение витрины и истории продаж. Без торговли."""

    market = Market.FRAGMENT
    native_currency = Currency.TON
    # Сделка требует подписи кошельком — ключей у бота нет.
    read_only = True

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__(base_url or settings.fragment_base_url)
        # Страницы публичные, ключ не нужен. Но разбор разметки — не
        # API: статус остаётся experimental, как у приватных площадок.
        self.capabilities = {
            Capability.SEARCH: CapabilityStatus.EXPERIMENTAL,
            Capability.HISTORY: CapabilityStatus.EXPERIMENTAL,
            # Сделка требует подписи кошельком. Ключей у бота нет.
            Capability.BUY: CapabilityStatus.UNAVAILABLE,
            Capability.LIST: CapabilityStatus.UNAVAILABLE,
            Capability.REPRICE: CapabilityStatus.UNAVAILABLE,
            Capability.CANCEL: CapabilityStatus.UNAVAILABLE,
            Capability.TRANSFER: CapabilityStatus.UNAVAILABLE,
            Capability.BALANCE: CapabilityStatus.UNAVAILABLE,
            Capability.INVENTORY: CapabilityStatus.UNAVAILABLE,
            Capability.RECONCILE: CapabilityStatus.UNAVAILABLE,
        }

    def _headers(self) -> dict[str, str]:
        """Обычные заголовки браузера: отдаётся разметка, не JSON."""
        return {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en",
        }

    async def _catalog(
        self,
        *,
        collection: str | None,
        model: str | None,
        backdrop: str | None,
        symbol: str | None,
        filter_: str,
        sort: str,
    ) -> list[FragmentCard]:
        """Запросить страницу каталога и разобрать её."""
        path = "/gifts"
        if collection:
            short = short_collection_name(collection)
            if not short:
                return []
            path = f"/gifts/{short}"

        params: dict[str, str] = {"filter": filter_, "sort": sort}
        # Атрибуты передаются как attr[Model]=["Lego"] — списком, даже
        # когда значение одно.
        for field, value in (
            ("Model", model), ("Backdrop", backdrop), ("Symbol", symbol)
        ):
            if value:
                params[f"attr[{field}]"] = json.dumps([value])

        html = await self.request_text("GET", path, params=params)

        # Проверяем, что каждый запрошенный атрибут действительно
        # применён: незнакомое значение Fragment просто игнорирует.
        for field, value in (
            ("Model", model), ("Backdrop", backdrop), ("Symbol", symbol)
        ):
            if value and not attribute_applied(html, field, value):
                raise FilterIgnored(
                    f"fragment: значение {field}={value!r} площадке "
                    f"неизвестно — вернулась бы вся коллекция"
                )

        return parse_cards(html)

    async def search(
        self,
        *,
        collection: str | None = None,
        model: str | None = None,
        backdrop: str | None = None,
        symbol: str | None = None,
        max_price: Decimal | None = None,
        limit: int = 100,
    ) -> list[ListingDTO]:
        """Активные лоты, самые дешёвые первыми.

        Первая запись — floor запрошенного среза: коллекции целиком
        либо конкретной модели, если она задана.
        """
        self._require(Capability.SEARCH)
        cards = await self._catalog(
            collection=collection, model=model, backdrop=backdrop,
            symbol=symbol, filter_="sale", sort="price_asc",
        )

        out: list[ListingDTO] = []
        for card in cards:
            if max_price is not None and card.price > max_price:
                # Список отсортирован по возрастанию — дальше только дороже.
                break
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=card.slug,
                    gift=GiftRef(
                        collection=card.collection,
                        number=card.number,
                        slug=card.slug,
                        model=model,
                        backdrop=backdrop,
                        symbol=symbol,
                    ),
                    price=card.price,
                    currency=card.currency,
                    raw={"fragment": True, "status": card.status},
                )
            )
            if len(out) >= limit:
                break
        return out

    async def history(
        self,
        *,
        collection: str | None = None,
        model: str | None = None,
        backdrop: str | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> list[SaleDTO]:
        """Состоявшиеся продажи, самые свежие первыми.

        Ровно то, чего нет больше нигде: цена сделки и её время. По
        ним считается скорость продаж — без неё риск всегда завышен.

        Карточки без времени сюда не попадают: продажа без даты не
        отличается от продажи годичной давности, а значит для расчёта
        скорости бесполезна.
        """
        self._require(Capability.HISTORY)
        cards = await self._catalog(
            collection=collection, model=model, backdrop=backdrop,
            symbol=symbol, filter_="sold", sort="ending",
        )

        out: list[SaleDTO] = []
        for card in cards:
            if card.moment is None:
                continue
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=f"{card.slug}@{card.moment.isoformat()}",
                    gift=GiftRef(
                        collection=card.collection,
                        number=card.number,
                        slug=card.slug,
                        model=model,
                        backdrop=backdrop,
                        symbol=symbol,
                    ),
                    price=card.price,
                    currency=card.currency,
                    happened_at=card.moment,
                    raw={"fragment": True},
                )
            )
            if len(out) >= limit:
                break
        return out
