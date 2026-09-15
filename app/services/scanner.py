"""Сканер рынков: поиск возможностей заработка.

Проход сканера:
    1. Обновить курс Stars/TON (FX-снапшот с таймстемпом).
    2. Собрать активные лоты по всем площадкам стратегий.
    3. Сохранить историю продаж как market facts.
    4. Оценить каждый лот: ROI после комиссий, риск, качество данных.
    5. Сохранить прошедшие фильтр как кандидатов.

Сканер ничего не покупает. Решение об исполнении принимает executor
в соответствии с режимом SAFE/SEMI/AUTO.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import (
    AdapterError,
    AuthRequired,
    Capability,
    CapabilityStatus,
    ListingDTO,
    RateLimited,
    SearchSkipped,
)
from app.adapters.base import GiftRef
from app.adapters.registry import get_adapter
from app.adapters.telegram_mtproto import TelegramAdapter
from app.db import session_scope
from app.enums import Currency, Market
from app.models import Candidate, utcnow
from app.services import gifts as gifts_service
from app.services import arbitrage
from app.services import marketdata, salestats
from app.services import strategy as strategy_service, valuation
from app.services.marketdata import MarketSnapshot

log = logging.getLogger(__name__)

def to_decimal_or_none(value: object) -> Decimal | None:
    """Мягкое приведение к Decimal для необязательных полей."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


#: По скольким коллекциям собирать историю продаж за проход.
HISTORY_COLLECTIONS = 20

#: Сколько живёт кандидат, прежде чем считать цену устаревшей.
CANDIDATE_TTL = dt.timedelta(minutes=10)

#: Ключ, под которым хранится отчёт о последнем проходе.
LAST_SCAN_KEY = "LAST_SCAN_REPORT"

#: Человеческие названия причин отсева — для интерфейса.
REJECTION_LABELS = {
    "filters": "не подходит под фильтры стратегии",
    "price_range": "цена вне коридора стратегии",
    "positions": "достигнут лимит открытых позиций",
    "confidence": "мало рыночных данных для оценки",
    "blockers": "сделка убыточна после комиссий",
    "roi": "прибыль ниже порога стратегии",
    "risk": "риск выше допустимого",
    "disagreement": "источники разошлись в цене — оценке нельзя верить",
    "no_fx": "нет свежего курса GRAM → Stars, цену не пересчитать",
    "cheap_reject": "цена заведомо выше любой оценки — отсеян без запроса",
}

#: Во сколько раз источники могут разойтись, прежде чем оценка
#: перестанет что-либо значить.
#:
#: Расхождение в разы — нормальная жизнь: официальная оценка Telegram
#: считается по всей коллекции, а floor на Portals — по конкретной
#: модели, и редкая модель стоит кратно дороже рядовой. Десятки раз
#: там тоже встречаются.
#:
#: А вот расхождение в сотни раз рынком не объясняется: floor
#: коллекции по определению не выше floor любой её модели. Значит
#: сломан масштаб — не та единица измерения, устаревший курс, чужая
#: валюта. Такие числа уже дважды выглядели находкой века: лот за 504
#: звезды при «оценке» 423 600 давал ROI 36 000%.
#:
#: Порог выбран с запасом: премия редкой модели к floor коллекции
#: доходит до десятков раз, поэтому настоящую находку он не тронет.
MAX_SOURCE_DISAGREEMENT = Decimal(50)


def active_plan(strategies: list) -> list[dict]:
    """Материализовать стратегии в простые словари.

    Сессия закрывается до асинхронных вызовов, поэтому объекты ORM
    дальше не живут: берём только нужные поля.
    """
    return [
        {
            "id": s.id,
            "name": s.name,
            "markets": [str(m).lower() for m in (s.markets or [])],
            "collections": list(s.collections or []),
            # Нужен дешёвой отсечке: она сравнивает с самым мягким
            # порогом из включённых стратегий.
            "min_roi": Decimal(s.min_roi or 0),
        }
        for s in strategies
    ]


def save_report(report: dict) -> None:
    """Сохранить отчёт о проходе, чтобы панель могла его показать."""
    import json

    from app.services import store

    try:
        store.set(LAST_SCAN_KEY, json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001 - отчёт не важнее самого скана
        log.debug("Не удалось сохранить отчёт скана: %s", exc)


def save_failure(exc: BaseException) -> None:
    """Записать, что проход сорвался, вместе с причиной."""
    save_report(
        {
            "listings": 0,
            "facts": 0,
            "candidates": 0,
            "markets": {},
            "rejections": {},
            # Список стратегий намеренно не заполняется: при сорванном
            # проходе мы о них ничего не знаем, а пустой список
            # прочитался бы как «стратегий нет».
            "started_at": utcnow().isoformat(timespec="seconds"),
            "finished_at": utcnow().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "note": "проход сорвался с ошибкой — смотрите journalctl -u gift-worker",
        }
    )


def last_report() -> dict | None:
    """Отчёт о последнем проходе сканера."""
    import json

    from app.services import store

    raw = store.get(LAST_SCAN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def refresh_fx(session: Session) -> Decimal | None:
    """Обновить курс TON->Stars.

    Курс выводится из сопоставимых цен: берём медианную цену подарков,
    выставленных и в Stars (Telegram), и в TON (внешние площадки).
    Пока сопоставимой пары нет, используется значение по умолчанию.
    """
    from app.models import Listing

    ton_prices = [
        Decimal(row.price)
        for row in session.query(Listing)
        .filter(Listing.is_active.is_(True), Listing.currency == Currency.TON)
        .limit(200)
        .all()
        if row.price
    ]
    if not ton_prices:
        return None

    # Прямой рыночный курс здесь получить неоткуда — фиксируем текущее
    # допущение снапшотом, чтобы расчёты были воспроизводимы.
    rate = marketdata.latest_fx(session, Currency.TON, Currency.STARS)
    if rate is None:
        # Курс обновляет отдельная задача из реальных источников;
        # подменять его здесь значило бы закрепить неверное значение.
        log.warning(
            "Курс TON->Stars ещё не получен. Расчёты приблизительны, "
            "пока не отработает обновление курсов."
        )
    return rate


def _search_adapters(market: Market) -> list:
    """Адаптеры, которыми можно вести поиск на площадке.

    Для Telegram это по одному адаптеру на каждый пригодный аккаунт:
    FloodWait считается по аккаунту, поэтому коллекции распределяются
    между ними и общий проход идёт быстрее, не приближаясь к порогу.
    """
    if market is not Market.TELEGRAM:
        return [get_adapter(market)]

    from app.adapters.registry import telegram_adapters

    with session_scope() as session:
        adapters = telegram_adapters(session)
    return adapters or [get_adapter(market)]


async def collect_listings(
    market: Market, *, collections: list[str], limit: int
) -> tuple[list[ListingDTO], list[str]]:
    """Собрать активные лоты площадки по списку коллекций.

    Returns:
        (лоты, причины). Причины нужны ровно для одного случая, который
        раньше выглядел одинаково при совершенно разных бедах: площадка
        вернула ноль лотов. Без них в панели оставалась догадка
        «проверьте токены и сеть», по которой ничего не найти.
    """
    notes: list[str] = []
    everything = _search_adapters(market)
    adapters = [a for a in everything if a.supports(Capability.SEARCH)]
    if not adapters:
        if not everything:
            notes.append("нет ни одного аккаунта для поиска")
        else:
            reason = {
                Market.TELEGRAM: "сессия не авторизована (gift-cli login)",
            }.get(market, "нет токена площадки — задайте в «Настройках»")
            notes.append(f"поиск недоступен: {reason}")
        return ([], notes)

    out: list[ListingDTO] = []
    targets: list[str | None] = list(collections) if collections else [None]
    #: Коллекции раздаются аккаунтам по кругу.
    exhausted: set[int] = set()
    empty: list[str] = []

    for index, collection in enumerate(targets):
        if len(exhausted) >= len(adapters):
            notes.append("все аккаунты выбыли, часть коллекций не осмотрена")
            break
        # Пропускаем аккаунты, которые уже упёрлись в лимит.
        for offset in range(len(adapters)):
            slot = (index + offset) % len(adapters)
            if slot not in exhausted:
                break
        else:
            break

        adapter = adapters[slot]
        label = getattr(adapter, "label", market.value)
        where = collection or "весь рынок"
        try:
            found = await adapter.search(collection=collection, limit=limit)
        except SearchSkipped as exc:
            # Беда одной коллекции, а не площадки: остальные смотрим.
            notes.append(str(exc))
            continue
        except RateLimited as exc:
            log.warning("%s (%s): лимит запросов, аккаунт пропущен: %s",
                        market.value, label, exc)
            notes.append(f"{label}: лимит запросов Telegram, аккаунт пропущен")
            exhausted.add(slot)
        except AuthRequired as exc:
            log.warning("%s (%s): нет доступа: %s", market.value, label, exc)
            notes.append(f"{label}: токен не принят ({exc})")
            exhausted.add(slot)
        except AdapterError as exc:
            log.warning("%s (%s): поиск недоступен: %s", market.value, label, exc)
            notes.append(f"{label}: {exc}")
            exhausted.add(slot)
        except Exception as exc:  # noqa: BLE001 - один аккаунт не роняет скан
            log.exception("%s (%s): ошибка поиска: %s", market.value, label, exc)
            notes.append(f"{label}: {type(exc).__name__}: {exc}")
            exhausted.add(slot)
        else:
            if found:
                out.extend(found)
            else:
                empty.append(where)

    if empty and not out:
        notes.append(
            "площадка ответила пусто по: " + ", ".join(empty[:5])
            + (" и др." if len(empty) > 5 else "")
        )
    return (out, notes)


async def collect_history(market: Market, *, collections: list[str]) -> int:
    """Загрузить историю продаж площадки в market facts."""
    adapter = get_adapter(market)
    if not adapter.supports(Capability.HISTORY):
        return 0

    saved = 0
    targets: list[str | None] = list(collections) if collections else [None]
    # История продаж — единственный источник скорости рынка, а без неё
    # риск каждой сделки получает надбавку «скорость продаж
    # неизвестна». Поэтому собираем по всем коллекциям стратегии, а не
    # по первым пяти, и сбой одной не обрывает остальные.
    failures = 0
    for collection in targets[:HISTORY_COLLECTIONS]:
        try:
            sales = await adapter.history(collection=collection, limit=100)
        except Exception as exc:  # noqa: BLE001 - одна коллекция не роняет сбор
            failures += 1
            log.debug("%s: история %s недоступна: %s", market.value, collection, exc)
            if failures >= 3:
                # Три подряд — это не коллекция, а площадка.
                log.warning(
                    "%s: история продаж недоступна, скорость рынка "
                    "останется неизвестной", market.value
                )
                break
            continue
        with session_scope() as session:
            saved += marketdata.record_facts(session, sales, market)
    return saved


PRICE_FIELDS = ("value", "floor_price", "average_price", "last_sale_price",
                "initial_sale_price")

# Какой площадке принадлежит цена из источника. Собственная выборка
# ("own") сюда не входит: она не про конкретный рынок.
SOURCE_MARKETS: dict[str, Market] = {
    "telegram_value_info": Market.TELEGRAM,
    "portals_attribute_floor": Market.PORTALS,
}


def _value_info_in_stars(session: Session, info: dict) -> dict | None:
    """Привести официальную оценку Telegram к Stars.

    ``getUniqueStarGiftValueInfo`` отдаёт цифры либо в Stars, либо в
    TON — с переходом резейла на TON второе встречается всё чаще. Без
    пересчёта floor в 5 TON встал бы рядом с ценой лота в Stars и
    подарок выглядел бы впятеро дешевле рынка.
    """
    currency = info.get("currency")
    if currency is None:
        # Валюта ответа незнакома. Число без валюты — не цена, и
        # подставить сюда звёзды значило бы назвать её наугад.
        return None
    if currency is Currency.STARS:
        return info
    out = dict(info)
    for field in PRICE_FIELDS:
        value = info.get(field)
        if value is None:
            continue
        in_stars = marketdata.to_stars(session, Decimal(str(value)), currency)
        if in_stars is None:
            # Курса нет — вся оценка непереводима, вместе с ней уходит
            # и источник: половина цифр в Stars, половина в TON хуже,
            # чем их отсутствие.
            return None
        out[field] = in_stars
    out["currency"] = Currency.STARS
    return out


def source_disagreement(
    sources: list[MarketSnapshot],
) -> tuple[Decimal, MarketSnapshot, MarketSnapshot] | None:
    """Во сколько раз разошлись источники и кто именно.

    Один источник проверить не на чем — ошибку в масштабе видно
    только рядом с другим. Поэтому сравниваются все, кто назвал цену.

    Returns:
        (во сколько раз, самый дешёвый, самый дорогой) либо None,
        когда сравнивать не с чем.
    """
    priced = [
        (snapshot.floor_price or snapshot.median_price, snapshot)
        for snapshot in sources
    ]
    priced = [(price, snap) for price, snap in priced if price and price > 0]
    if len(priced) < 2:
        return None

    low_price, low = min(priced, key=lambda item: item[0])
    high_price, high = max(priced, key=lambda item: item[0])
    return (high_price / low_price, low, high)


def venue_floor(sources: list[MarketSnapshot], market: Market) -> Decimal | None:
    """Минимальная цена таких лотов на этой самой площадке.

    Потолок для цены продажи. Когда справедливую цену дала чужая
    площадка, без него расчёт обещает продажу по цене, которой на
    нашей площадке нет: там тот же подарок может стоить в разы
    дешевле, и ROI получается кратным на пустом месте.
    """
    wanted = SOURCE_MARKETS
    for snapshot in sources:
        if wanted.get(snapshot.source) is not market:
            continue
        floor = snapshot.floor_price or snapshot.median_price
        if floor and floor > 0:
            return floor
    return None


def live_prices(sources: list[MarketSnapshot]) -> dict[Market, Decimal]:
    """Цены в Stars, которые площадки назвали при сборе источников.

    Нужны, чтобы прикинуть продажу на площадке, куда сканер за лотами
    не ходил: floor модели на Portals спрашивается по любому подарку с
    моделью, даже если ни одного лота этой коллекции мы там не видели.
    """
    out: dict[Market, Decimal] = {}
    for snapshot in sources:
        market = SOURCE_MARKETS.get(snapshot.source)
        if market is None:
            continue
        price = snapshot.floor_price or snapshot.median_price
        if price and price > 0:
            out[market] = price
    return out


#: Запас к дешёвому потолку перед отсечкой.
#:
#: Потолок считается по тому, что уже лежит в базе и в кэше, а полный
#: сбор источников может найти цену выше. Запас делает отсечку заведомо
#: щедрой: лишний лот пройдёт в дорогую проверку, но настоящая находка
#: не потеряется молча. Экономия от этого почти не страдает — мимо
#: порога такие лоты проходят не на проценты, а в разы.
CHEAP_MARGIN = Decimal("1.3")


def cheap_ceiling(
    session: Session, dto: ListingDTO, *, margin: Decimal | None = None
) -> Decimal | None:
    """Оптимистичная верхняя граница цены продажи — без единого запроса.

    Оценка лота стоит одного обращения к Telegram на каждый лот, и
    именно в них уходит почти всё время прохода. Но большинство лотов
    не проходят порог с запасом в разы, и чтобы это понять, сеть не
    нужна: хватит того, что уже собрано.

    Берётся максимум из всего известного — намеренно завышенный, чтобы
    отсечка могла только ошибиться в сторону лишней работы.

    Returns:
        Цена в Stars, выше которой продать точно не выйдет, либо None,
        когда не известно ничего и судить не на чем.
    """
    from app.services import salestats

    best: Decimal | None = None

    def offer(value) -> None:
        nonlocal best
        if value and value > 0 and (best is None or value > best):
            best = Decimal(value)

    # Свои наблюдения: и по этой модели, и по коллекции целиком.
    for model in (dto.gift.model, None):
        for market in (dto.market, None):
            own = marketdata.snapshot_for(
                session,
                collection=dto.gift.collection,
                model=model,
                market=market,
            )
            offer(own.floor_price)
            offer(own.median_price)

    # Floor'ы Portals — только если кэш уже прогрет, греть его здесь
    # нельзя: это был бы тот самый запрос, который мы экономим.
    adapter = get_adapter(Market.PORTALS)
    cached = getattr(adapter, "cached_floors", None)
    if cached is not None:
        floors = cached(dto.gift.collection)
        if floors:
            attr_floor, _ = rarest_attribute_floor(floors, dto.gift)
            if attr_floor:
                offer(marketdata.to_stars(session, attr_floor, Currency.TON))

    # Состоявшиеся сделки — они уже в базе.
    stats = salestats.sale_stats(
        session, collection=dto.gift.collection, model=dto.gift.model
    )
    offer(stats.high)
    offer(stats.median)

    if best is None:
        return None
    return best * (CHEAP_MARGIN if margin is None else margin)


def hopeless(
    session: Session, dto: ListingDTO, price_stars: Decimal, need_roi: Decimal
) -> bool:
    """Безнадёжен ли лот даже при самой щедрой оценке.

    Считает по той же арифметике, что и полная оценка, но с потолком
    вместо справедливой цены. Если и так не дотягивает до самого
    мягкого порога из включённых стратегий — сеть тревожить незачем.
    """
    ceiling = cheap_ceiling(session, dto)
    if ceiling is None:
        # Не известно ничего: судить не на чем, идём длинным путём.
        return False

    buy_fees = valuation.fees_in(
        session, valuation.get_fees(session, dto.market), Currency.STARS
    )
    sell_fees = valuation.fees_in(
        session, valuation.get_fees(session, dto.market), Currency.STARS
    )
    cost = valuation.total_cost_of(price_stars, buy_fees)
    if cost <= 0:
        return False
    proceeds = valuation.net_proceeds_from(ceiling, sell_fees)
    return ((proceeds - cost) / cost) < need_roi


def rarest_attribute_floor(
    floors: dict, gift: "GiftRef"
) -> tuple[Decimal | None, str | None]:
    """Самый дорогой из floor'ов признаков этого подарка.

    Площадка считает минимальную цену отдельно по модели, символу и
    фону. У подарка все три сразу, и стоит он не меньше самого дорогого
    из них: чтобы получить его признак, покупателю иначе пришлось бы
    взять самый дешёвый лот с этим признаком — а он и стоит floor.

    Раньше брался только floor модели. Подарок с рядовой моделью и
    редким фоном оценивался по модели и выглядел дорогим — его просто
    пропускали, хотя один фон стоил вчетверо больше.

    Returns:
        (floor в валюте площадки, какой признак его дал).
    """
    best: Decimal | None = None
    reason: str | None = None
    for section, value, label in (
        ("models", gift.model, "модель"),
        ("backdrops", gift.backdrop, "фон"),
        ("symbols", gift.symbol, "символ"),
    ):
        if not value:
            continue
        raw = (floors.get(section) or {}).get(value)
        if not raw:
            continue
        price = Decimal(str(raw))
        if price <= 0:
            continue
        if best is None or price > best:
            best, reason = price, f"{label} {value}"
    return best, reason


async def gather_sources(
    session: Session, dto: ListingDTO
) -> list[MarketSnapshot]:
    """Собрать всё, что известно об этом подарке, из каждого источника.

    Источники отвечают на разные вопросы и по-разному надёжны:

    * официальная оценка Telegram — floor, средняя и последняя продажа
      от самой площадки;
    * floor модели на Portals — самая точная цена редкой модели,
      доступная сразу;
    * собственные наблюдения — единственный источник скорости продаж.

    Раньше брался первый сработавший, остальные отбрасывались, и
    картина получалась однобокой. Здесь собираются все, чтобы решение
    опиралось на полную выборку, а не на то, что попалось первым.

    Цены при этом НЕ усредняются между площадками: один и тот же
    подарок стоит на них по-разному, и среднее было бы числом, по
    которому нельзя ни купить, ни продать.
    """
    sources: list[MarketSnapshot] = []

    # Собственные наблюдения нужны сразу: только они говорят, сколько
    # таких лотов реально выставлено. Площадки этого числа не дают, а
    # без него ликвидность в риске считать не на чем.
    own = marketdata.snapshot_for(
        session, collection=dto.gift.collection, model=dto.gift.model
    )

    # 1. Официальная оценка Telegram — доступна по любому подарку,
    #    у которого есть slug, независимо от того, где он продаётся.
    slug = dto.gift.slug or (dto.external_id if dto.market is Market.TELEGRAM else None)
    if slug:
        adapter = get_adapter(Market.TELEGRAM)
        if isinstance(adapter, TelegramAdapter) and adapter.supports(Capability.SEARCH):
            try:
                info = _value_info_in_stars(
                    session, await adapter.value_info(slug)
                )
                if info and (info.get("floor_price") or info.get("average_price")):
                    sources.append(
                        marketdata.snapshot_from_telegram(
                            info,
                            collection=dto.gift.collection,
                            model=dto.gift.model,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - источник необязательный
                log.debug("value_info для %s недоступен: %s", slug, exc)

    # 2. Floor признаков на Portals — по самому дорогому из них.
    if dto.gift.model or dto.gift.backdrop or dto.gift.symbol:
        adapter = get_adapter(Market.PORTALS)
        floors = getattr(adapter, "attribute_floors", None)
        if floors is not None:
            try:
                data = await floors(dto.gift.collection)
                attr_floor, why = rarest_attribute_floor(data, dto.gift)
                if attr_floor:
                    floor_stars = marketdata.to_stars(
                        session, attr_floor, Currency.TON
                    )
                    if floor_stars:
                        log.debug(
                            "%s: floor признака — %s (%s)",
                            dto.external_id, attr_floor, why,
                        )
                        sources.append(
                            marketdata.snapshot_from_attribute_floor(
                                collection=dto.gift.collection,
                                model=dto.gift.model,
                                model_floor=floor_stars,
                                listed_count=own.active_listings,
                                models_listed=len(data.get("models") or {}),
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                log.debug("Portals: floor признаков недоступен: %s", exc)

    # 3. Состоявшиеся продажи на Fragment — цена сделки и её время.
    #    Единственный источник, который отвечает не «почём просят», а
    #    «почём купили». Берётся из базы: страницы обходит отдельная
    #    задача, здесь сетевого запроса нет.
    fragment = marketdata.snapshot_for(
        session,
        collection=dto.gift.collection,
        model=dto.gift.model,
        market=Market.FRAGMENT,
    )
    if fragment.sample_size:
        sources.append(fragment)

    # 4. Собственные наблюдения.
    if own.median_price or own.floor_price or own.velocity_per_day:
        sources.append(own)

    return sources


def audit_view(sources: list[MarketSnapshot]) -> list[dict]:
    """Сводка по источникам для обоснования решения.

    Показывает, что именно сказал каждый источник, — чтобы решение
    можно было перепроверить, а не принимать на веру.
    """
    return [
        {
            "source": s.source,
            "median": str(s.median_price.quantize(Decimal("1")))
            if s.median_price else None,
            "floor": str(s.floor_price.quantize(Decimal("1")))
            if s.floor_price else None,
            "sample": s.sample_size,
            "listings": s.active_listings,
            "confidence": s.confidence.value,
            "velocity_per_day": s.velocity_per_day or None,
            "days_to_sell": s.days_to_sell,
            "newest": s.newest.isoformat(timespec="minutes") if s.newest else None,
        }
        for s in sources
    ]


def choose_primary(
    sources: list[MarketSnapshot], market: Market
) -> MarketSnapshot | None:
    """Выбрать срез, по которому считать сделку.

    Усреднять между источниками нельзя: они описывают разные рынки.
    Поэтому берётся один, самый надёжный для цены, а остальные идут в
    сводку аудита.

    Порядок: сперва источник той площадки, где лот и куплен, потом
    чужие. Цена — свойство площадки, а не подарка: floor модели на
    Portals точен для Portals и ничего не говорит о том, за сколько
    этот подарок уйдёт в Telegram.

    Дальше — floor модели на Portals (он точнее всего описывает
    редкую модель), официальная оценка Telegram, состоявшиеся сделки
    на Fragment и собственная выборка.

    Сделки Fragment стоят ниже площадочных оценок намеренно. Они
    честнее по природе — это цена, по которой заплатили, а не по
    которой просят, — но сняты с другой площадки, а туда подарок
    попадает через вывод на блокчейн и потому обычно стоит дешевле.
    Взять их за справедливую цену для лота в Telegram значит занизить
    её. Занижение безопасно (кандидатов станет меньше, а не больше),
    и на этом основании Fragment всё же идёт впереди собственной
    выборки: несколько десятков чужих сделок содержательнее пары
    наших наблюдений.
    """
    if not sources:
        return None

    priority = {
        # Своя площадка всегда первая. Floor модели на Portals точнее
        # всего для редкой модели — но это цена **на Portals**, и для
        # лота в Telegram она не справедливая оценка, а чужая. Раньше
        # она стояла первой безусловно, и телеграмный лот оценивался
        # по портальской цене: ROI выходил кратным, а продать по такой
        # цене в Telegram было нельзя.
        "portals_attribute_floor": 0 if market is Market.PORTALS else 2,
        "telegram_value_info": 0 if market is Market.TELEGRAM else 3,
        f"market:{Market.FRAGMENT.value}": 4,
    }
    return min(sources, key=lambda s: priority.get(s.source, 5))


async def snapshot_for_listing(
    session: Session, dto: ListingDTO
) -> MarketSnapshot:
    """Построить срез рынка для конкретного лота.

    Для Telegram приоритет у официальной оценки
    ``payments.getUniqueStarGiftValueInfo``: floor, средняя цена и
    последняя продажа приходят от самой площадки.
    """
    if dto.market is Market.TELEGRAM and dto.external_id:
        adapter = get_adapter(Market.TELEGRAM)
        if isinstance(adapter, TelegramAdapter):
            try:
                info = await adapter.value_info(dto.external_id)
                if info.get("floor_price") or info.get("average_price"):
                    return marketdata.snapshot_from_telegram(
                        info, collection=dto.gift.collection, model=dto.gift.model
                    )
            except Exception as exc:  # noqa: BLE001 - откатываемся на свою выборку
                log.debug("value_info для %s недоступен: %s", dto.external_id, exc)

    if dto.market is Market.PORTALS and dto.gift.model:
        # Floor по модели — самая точная оценка, доступная сразу.
        adapter = get_adapter(Market.PORTALS)
        floors = getattr(adapter, "attribute_floors", None)
        if floors is not None:
            try:
                data = await floors(dto.gift.collection)
                model_floor = (data.get("models") or {}).get(dto.gift.model)
                if model_floor:
                    # Portals отдаёт floor в TON, а вся оценка сделки
                    # идёт в Stars. Без приведения справедливая цена
                    # оказывалась в 65 раз ниже цены покупки, и любой
                    # лот выглядел безнадёжно переоценённым.
                    floor_stars = marketdata.to_stars(
                        session, Decimal(str(model_floor)), Currency.TON
                    )
                    collection_floor = to_decimal_or_none(
                        (dto.raw or {}).get("floor_price")
                    )
                    if collection_floor is not None:
                        collection_floor = marketdata.to_stars(
                            session, collection_floor, Currency.TON
                        )
                    if floor_stars:
                        seen_now = marketdata.snapshot_for(
                            session,
                            collection=dto.gift.collection,
                            model=dto.gift.model,
                        )
                        snapshot = marketdata.snapshot_from_attribute_floor(
                            collection=dto.gift.collection,
                            model=dto.gift.model,
                            model_floor=floor_stars,
                            collection_floor=collection_floor,
                            listed_count=seen_now.active_listings,
                            models_listed=len(data.get("models") or {}),
                        )
                        # Floor площадки даёт цену, но молчит о том,
                        # как быстро такие лоты уходят. Скорость берём
                        # из собственных наблюдений: без неё каждая
                        # сделка получает надбавку к риску «скорость
                        # продаж неизвестна».
                        return marketdata.with_observed_velocity(
                            session, snapshot
                        )
            except Exception as exc:  # noqa: BLE001 - откат на историю
                log.debug("Portals: floor модели недоступен: %s", exc)

    return marketdata.snapshot_for(
        session, collection=dto.gift.collection, model=dto.gift.model
    )


async def scan_once() -> dict:
    """Один полный проход сканера.

    Returns:
        Сводка: сколько лотов просмотрено и сколько кандидатов создано.
    """
    started = utcnow()
    # Отмечаем, что проход пошёл. Без этого долгий скан выглядит в
    # панели точно так же, как остановленный воркер, и человек идёт
    # искать сбой, которого нет.
    #
    # Пометка добавляется к прошлому отчёту, а не заменяет его: иначе
    # пока идёт проход, панель теряет все прежние цифры и сообщает,
    # например, что стратегий нет вовсе.
    save_report(
        {
            **(last_report() or {}),
            "running": True,
            "started_at": started.isoformat(timespec="seconds"),
        }
    )
    report: dict = {
        "listings": 0,
        "facts": 0,
        "candidates": 0,
        "markets": {},
        "rejections": {},
        "strategies": [],
        "started_at": started.isoformat(timespec="seconds"),
    }

    with session_scope() as session:
        strategies = strategy_service.active_strategies(session)
        report["strategies"] = [s.name for s in strategies]
        if not strategies:
            report["note"] = (
                "нет включённых стратегий — включите на странице «Стратегии» в боте"
            )
            report["finished_at"] = utcnow().isoformat(timespec="seconds")
            save_report(report)
            log.info("Нет включённых стратегий — сканирование пропущено")
            return report
        plan = active_plan(strategies)

    wanted_markets: dict[Market, set[str]] = {}
    for item in plan:
        for market_name in item["markets"] or [Market.TELEGRAM.value]:
            try:
                market = Market(market_name)
            except ValueError:
                continue
            wanted_markets.setdefault(market, set()).update(item["collections"])

    # --- сбор данных ---
    all_listings: list[ListingDTO] = []
    for market, collections in wanted_markets.items():
        rows, notes = await collect_listings(
            market, collections=sorted(collections), limit=200
        )
        report["markets"][market.value] = len(rows)
        if notes:
            report.setdefault("market_notes", {})[market.value] = notes
        all_listings.extend(rows)

        facts = await collect_history(market, collections=sorted(collections))
        report["facts"] += facts

    report["listings"] = len(all_listings)
    if not all_listings:
        known = report.get("market_notes") or {}
        if known:
            # Причина известна — незачем отправлять человека гадать.
            report["note"] = "ни одного лота. " + "; ".join(
                f"{market}: {notes[0]}" for market, notes in known.items()
            )
        else:
            report["note"] = (
                "площадки не вернули ни одного лота — проверьте токены и сеть "
                "(gift-cli probe)"
            )
        report["finished_at"] = utcnow().isoformat(timespec="seconds")
        save_report(report)
        return report

    # Сколько заняли сбор лотов и сколько — их оценка. Разбивка нужна
    # не для отчётности: медленная половина и есть то, что стоит
    # ускорять, а без замера это гадание.
    collected_at = utcnow()
    report["collect_sec"] = round((collected_at - started).total_seconds(), 1)

    # --- сохранение лотов ---
    with session_scope() as session:
        await refresh_fx(session)
        for dto in all_listings:
            gifts_service.upsert_listing(session, dto)
        seen: dict[Market, set[str]] = {}
        for dto in all_listings:
            seen.setdefault(dto.market, set()).add(dto.external_id)
        for market, ids in seen.items():
            gifts_service.deactivate_missing(session, market, ids)

    # --- оценка ---
    created = 0
    rejections: Counter[str] = Counter()
    for dto in all_listings:
        try:
            made, reasons = await evaluate_listing(dto, plan)
            created += made
            rejections.update(reasons)
        except Exception as exc:  # noqa: BLE001 - один лот не роняет скан
            log.exception("Ошибка оценки лота %s: %s", dto.external_id, exc)

    # --- разница цен между площадками ---
    # Считается отдельно от кандидатов: связка требует ручного
    # переноса подарка, поэтому она подсказка владельцу, а не заявка
    # на исполнение.
    if arbitrage.enabled():
        with session_scope() as session:
            spreads = arbitrage.find(session, all_listings)
        report["spreads"] = [s.as_dict() for s in spreads[:20]]
        if spreads:
            log.info(
                "Разница цен между площадками: %s связок, лучшая %.1f%%",
                len(spreads),
                float(spreads[0].net_roi) * 100,
            )

    report["candidates"] = created
    report["rejections"] = dict(rejections)
    report["finished_at"] = utcnow().isoformat(timespec="seconds")
    report["duration_sec"] = int((utcnow() - started).total_seconds())
    report["evaluate_sec"] = round(
        (utcnow() - collected_at).total_seconds(), 1
    )
    if all_listings:
        # Время на один лот — то число, по которому видно, во что
        # упрётся более частый обход.
        report["per_listing_ms"] = int(
            (utcnow() - collected_at).total_seconds() * 1000 / len(all_listings)
        )
    if not created and rejections:
        top = rejections.most_common(1)[0][0]
        report["note"] = (
            f"подходящих лотов нет, чаще всего: "
            f"{REJECTION_LABELS.get(top, top)}"
        )
    save_report(report)
    log.info(
        "Скан завершён: лотов %s, фактов %s, кандидатов %s",
        report["listings"],
        report["facts"],
        created,
    )
    return report


def price_gap(fair_value: Decimal, price: Decimal) -> Decimal | None:
    """На сколько лот дешевле справедливой цены.

    Не то же самое, что ROI. ROI считается после комиссий и по той
    цене, по которой мы рассчитываем продать (а она бывает ниже
    оценки, если floor ниже медианы). Разрыв отвечает на более простой
    вопрос — «насколько ниже рынка», — и именно им меряют удачную
    покупку в каналах находок.
    """
    if fair_value <= 0:
        return None
    return (fair_value - price) / fair_value


def first_seen(session: Session, dto: ListingDTO) -> "dt.datetime | None":
    """Когда этот лот впервые попался нам на глаза.

    Лот перезаписывается на каждом проходе, поэтому «когда увидели в
    последний раз» не годится: разница с моментом покупки и есть наше
    опоздание, а по свежему seen_at она всегда близка к нулю.
    """
    from app.models import Listing

    row = (
        session.query(Listing)
        .filter_by(market=dto.market, external_id=dto.external_id)
        .first()
    )
    return getattr(row, "created_at", None) if row is not None else None


def _sale_hint(best: dict | None) -> dict | None:
    """Привести подсказку о продаже к виду, пригодному для JSON."""
    if not best:
        return None
    return {
        "market": best["market"],
        "sale_price": str(best["sale_price"].quantize(Decimal("1"))),
        "net_roi": f"{best['net_roi']:.1%}",
        "net_profit": str(best["net_profit"].quantize(Decimal("1"))),
        "listings": best["listings"],
        "basis": best.get("basis"),
        "note": best["note"],
    }


async def evaluate_listing(
    dto: ListingDTO, plan: list[dict]
) -> tuple[int, Counter[str]]:
    """Оценить лот по всем подходящим стратегиям.

    Returns:
        (сколько кандидатов создано, причины отсева)
    """
    from app.models import Strategy

    created = 0
    rejections: Counter[str] = Counter()
    with session_scope() as session:
        price_stars = marketdata.to_stars(session, dto.price, dto.currency)
        if price_stars is None or price_stars <= 0:
            # Чаще всего это устаревший курс. Молчаливый пропуск
            # выглядел бы как «лотов нет», и человек искал бы поломку
            # в площадках вместо настроек.
            if dto.currency is not Currency.STARS:
                rejections["no_fx"] += 1
            return (0, rejections)

        # Дешёвая отсечка перед дорогой частью: почти все лоты не
        # проходят порог с запасом в разы, и понять это можно по
        # накопленным данным, не обращаясь к площадкам.
        need_roi = min(
            (Decimal(item.get("min_roi") or 0) for item in plan),
            default=Decimal(0),
        )
        if hopeless(session, dto, price_stars, need_roi):
            rejections["cheap_reject"] += len(plan)
            return (0, rejections)

        sources = await gather_sources(session, dto)
        snapshot = choose_primary(sources, dto.market)
        if snapshot is None:
            snapshot = await snapshot_for_listing(session, dto)
        snapshot = marketdata.with_observed_velocity(session, snapshot)
        audit = audit_view(sources)
        known = live_prices(sources)
        own_floor = venue_floor(sources, dto.market)

        # Источники, разошедшиеся на порядки, не оценка, а поломка
        # масштаба: не та единица, устаревший курс, чужая валюта.
        # Выбрать из них «самый выгодный» — значит построить сделку на
        # сломанном числе, и выглядеть она будет тем убедительнее, чем
        # сильнее поломка.
        gap = source_disagreement(sources)
        if gap is not None and gap[0] > MAX_SOURCE_DISAGREEMENT:
            times, low, high = gap
            log.warning(
                "%s %s: источники разошлись в %.0f раз — %s даёт %s, "
                "%s даёт %s. Лот пропущен.",
                dto.gift.collection, dto.gift.model or "",
                float(times),
                low.source, low.floor_price or low.median_price,
                high.source, high.floor_price or high.median_price,
            )
            rejections["disagreement"] += len(plan)
            return (0, rejections)
        # Разброс цен сделок: бывают ли в этом виде подарков дешёвые
        # входы вообще. На отбор не влияет — это мера угодий, а не
        # конкретного лота, — но объясняет, почему лот дешёвый.
        stats = salestats.sale_stats(
            session, collection=dto.gift.collection, model=dto.gift.model
        )
        seen_first = first_seen(session, dto)
        adapter = get_adapter(dto.market)
        is_official = (
            adapter.status_of(Capability.BUY) is CapabilityStatus.SUPPORTED
        )

        for item in plan:
            strategy = session.get(Strategy, item["id"])
            if strategy is None or not strategy.is_enabled:
                continue

            ok, reason = strategy_service.matches_filters(strategy, dto)
            if not ok:
                rejections["filters"] += 1
                continue
            ok, reason = strategy_service.price_in_range(strategy, price_stars)
            if not ok:
                rejections["price_range"] += 1
                continue
            ok, reason = strategy_service.can_open_position(session, strategy)
            if not ok:
                rejections["positions"] += 1
                log.debug("Стратегия %s: %s", strategy.name, reason)
                continue

            required = strategy_service.required_confidence(strategy)
            if not marketdata.confidence_at_least(snapshot.confidence, required):
                rejections["confidence"] += 1
                continue

            result = valuation.evaluate(
                session,
                buy_market=dto.market,
                buy_price=price_stars,
                # Продаём там же, где купили: кросс-рыночная сделка
                # не атомарна и требует отдельного подтверждения.
                sell_market=dto.market,
                snapshot=snapshot,
                is_official_api=is_official,
                target_markup=Decimal(strategy.sell_markup or 0),
                venue_floor=own_floor,
            )

            if result.blockers:
                rejections["blockers"] += 1
                continue
            if result.net_roi < Decimal(strategy.min_roi):
                rejections["roi"] += 1
                continue
            if result.risk_score > strategy.max_risk:
                rejections["risk"] += 1
                continue

            # Где выгоднее продать: комиссии площадок различаются в
            # разы. Это подсказка — перенос подарка ручной, — поэтому
            # на отбор кандидата не влияет и живёт в обосновании.
            elsewhere = valuation.best_sale_market(
                session,
                buy_market=dto.market,
                buy_price=price_stars,
                collection=dto.gift.collection,
                model=dto.gift.model,
                known_prices=known,
            )
            if elsewhere and elsewhere["net_roi"] > result.net_roi:
                result.reasons.append(elsewhere["note"])
            else:
                elsewhere = None

            gift = gifts_service.upsert_gift(session, dto.gift)
            exists = (
                session.query(Candidate)
                .filter_by(
                    strategy_id=strategy.id,
                    market=dto.market,
                    listing_external_id=dto.external_id,
                    state="pending",
                )
                .first()
            )
            if exists is not None:
                # Обновляем цену и оценку вместо создания дубликата.
                exists.price_stars = price_stars
                exists.price_native = dto.price
                exists.native_currency = dto.currency
                exists.fair_value_stars = result.fair_value
                exists.net_roi = result.net_roi
                exists.risk_score = result.risk_score
                exists.confidence = result.confidence
                exists.discount = price_gap(result.fair_value, price_stars)
                exists.days_to_sell = snapshot.days_to_sell
                exists.sale_velocity = snapshot.velocity_per_day or None
                if exists.first_seen_at is None:
                    exists.first_seen_at = seen_first
                exists.rationale = {
                    **result.as_dict(),
                    "market": snapshot.as_dict(),
                    "sources": audit,
                    "better_sale": _sale_hint(elsewhere),
                    "sales": stats.as_dict(),
                }
                exists.expires_at = utcnow() + CANDIDATE_TTL
                continue

            session.add(
                Candidate(
                    strategy_id=strategy.id,
                    gift_id=gift.id,
                    market=dto.market,
                    listing_external_id=dto.external_id,
                    price_stars=price_stars,
                    price_native=dto.price,
                    native_currency=dto.currency,
                    fair_value_stars=result.fair_value,
                    net_roi=result.net_roi,
                    risk_score=result.risk_score,
                    confidence=result.confidence,
                    discount=price_gap(result.fair_value, price_stars),
                    days_to_sell=snapshot.days_to_sell,
                    sale_velocity=snapshot.velocity_per_day or None,
                    first_seen_at=seen_first,
                    rationale={
                        **result.as_dict(),
                        "market": snapshot.as_dict(),
                        "sources": audit,
                        "better_sale": _sale_hint(elsewhere),
                        "sales": stats.as_dict(),
                    },
                    state="pending",
                    expires_at=utcnow() + CANDIDATE_TTL,
                )
            )
            created += 1
            log.info(
                "Кандидат: %s на %s за %s Stars, ROI %.1f%%, риск %s",
                gifts_service.describe(gift),
                dto.market.value,
                price_stars,
                float(result.net_roi) * 100,
                result.risk_score,
            )
    return (created, rejections)


def expire_candidates(session: Session) -> int:
    """Пометить протухших кандидатов: их цена больше не актуальна."""
    now = utcnow()
    rows = (
        session.query(Candidate)
        .filter(Candidate.state == "pending", Candidate.expires_at < now)
        .all()
    )
    for row in rows:
        row.state = "expired"
    return len(rows)
