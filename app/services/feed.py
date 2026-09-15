"""Разбор канала находок: какие коллекции реально приносят прибыль.

Чужой бот публикует свои удачные покупки постами вида:

    #находка_дня
    😍 SpringBasket-40494 - за 16.43 GRAM (Оценка: 26.00 GRAM)
    💪 TamaGadget-18389 - за 102.60 GRAM (Продано: 215.00 GRAM)

Отсюда берётся то единственное, ради чего это стоит читать: **список
коллекций, в которых недооценённые лоты появляются на практике**.
Сканер ограничен по пропускной способности, и распылять её на сотню
коллекций — значит смотреть каждую раз в час. Десяток коллекций из
канала осматривается за секунды.

Чего эти посты НЕ доказывают, и это важно:

* Пост показывает лучшие 3 покупки из 589. Про остальные 586 и про
  убытки там не пишут — это витрина, а не отчёт.
* «Оценка» — это их собственная прикидка, ровно такая же
  нереализованная цифра, какую считает наш бот. Доказательством
  прибыли служит только «Продано».
* Пост выходит про вчера: конкретные лоты давно куплены.

Поэтому коллекции ранжируются по **реализованным** продажам, а
«Оценка» идёт с пониженным весом. Список коллекций — это подсказка,
куда смотреть, а не разрешение покупать: решение по каждому лоту
по-прежнему принимает обычная оценка сделки.

GRAM — это TON: 15 июня 2026 сеть переименовала токен, курс 1:1,
поэтому цены из постов кладутся в TON без пересчёта.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.enums import Currency

log = logging.getLogger(__name__)

#: Метка поста с находками.
HASHTAG = "#находка_дня"

#: GRAM — переименованный TON, курс 1:1 (15.06.2026).
FEED_CURRENCY = Currency.TON

#: Одна находка в посте. Пример строки:
#:     😍 SpringBasket-40494 - за 16.43 GRAM (Оценка: 26.00 GRAM)
#: Пробелы и переносы строк заранее схлопнуты, поэтому здесь \s*
#: достаточно: в постах цена регулярно отрывается от слова GRAM.
FIND_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9]*)"        # SpringBasket
    r"\s*[-–—]\s*(?P<number>\d+)"            # -40494
    r"\s*[-–—]\s*за\s*"                      # - за
    r"(?P<price>\d+(?:[.,]\d+)?)"            # 16.43
    r"\s*GRAM\s*\(\s*"
    r"(?P<kind>Оценка|Продано)\s*:\s*"
    r"(?P<value>\d+(?:[.,]\d+)?)"            # 26.00
    r"\s*GRAM\s*\)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Find:
    """Одна находка из поста."""

    collection: str
    number: int
    price: Decimal
    value: Decimal
    #: True — подарок реально продан, False — это их собственная оценка.
    realized: bool
    currency: Currency = FEED_CURRENCY

    @property
    def margin(self) -> Decimal:
        """Во сколько раз оценка выше цены покупки."""
        return (self.value / self.price) if self.price > 0 else Decimal(0)

    def as_dict(self) -> dict:
        """Представление для интерфейса."""
        return {
            "collection": self.collection,
            "number": self.number,
            "price": str(self.price),
            "value": str(self.value),
            "realized": self.realized,
            "margin": f"{self.margin:.2f}x",
        }


def _decimal(raw: str) -> Decimal | None:
    """Число из поста: запятая и точка равноправны."""
    try:
        return Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def split_camel(name: str) -> str:
    """Развернуть SpringBasket в «Spring Basket».

    Площадки пишут коллекции с пробелами, а в постах они слитно.
    Разбор по заглавным буквам покрывает обычные названия; для
    нестандартных есть сверка с уже виденными коллекциями.
    """
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|\d+", name)
    return " ".join(parts) if parts else name


def normalize(text: str) -> str:
    """Схлопнуть переносы и лишние пробелы.

    В постах цена и слово GRAM регулярно оказываются на разных
    строках — без этого половина находок не распозналась бы.
    """
    return re.sub(r"\s+", " ", text or "").strip()


def parse_post(text: str, *, known: dict[str, str] | None = None) -> list[Find]:
    """Разобрать один пост в список находок.

    Args:
        known: соответствие «слитное имя в нижнем регистре» -> реальное
            название коллекции. Берётся из уже виденных на площадках
            коллекций и точнее разбора по заглавным буквам.

    Returns:
        Находки в порядке появления; нераспознанные строки молча
        пропускаются — пост может содержать что угодно ещё.
    """
    lookup = known or {}
    finds: list[Find] = []

    for match in FIND_RE.finditer(normalize(text)):
        price = _decimal(match.group("price"))
        value = _decimal(match.group("value"))
        if price is None or value is None or price <= 0:
            continue

        raw_name = match.group("name")
        collection = lookup.get(raw_name.lower()) or split_camel(raw_name)

        finds.append(
            Find(
                collection=collection,
                number=int(match.group("number")),
                price=price,
                value=value,
                realized=match.group("kind").lower() == "продано",
            )
        )

    return finds


def known_collections(session: Session) -> dict[str, str]:
    """Коллекции, уже виденные на площадках: слитное имя -> настоящее.

    Даёт точное название там, где разбор по заглавным буквам ошибся бы
    (например, «B-Day Candle» против «BDayCandle»).
    """
    from app.models import Gift

    rows = session.query(Gift.collection).distinct().all()
    out: dict[str, str] = {}
    for (name,) in rows:
        if not name:
            continue
        out[re.sub(r"[^a-z0-9]", "", name.lower())] = name
    return out


# ----------------------------------------------------------------------
# Чтение канала
# ----------------------------------------------------------------------
#: Настройки канала.
KEY_CHANNEL = "FEED_CHANNEL"
KEY_ENABLED = "FEED_ENABLED"
KEY_DEPTH = "FEED_DEPTH"
KEY_LAST_SYNC = "FEED_LAST_SYNC"

#: Сколько последних сообщений читать за проход.
DEFAULT_DEPTH = 50

#: За какой срок находки учитываются при ранжировании. Рынок меняется,
#: и коллекция, кормившая месяц назад, сегодня может быть вычищена.
RANK_WINDOW = dt.timedelta(days=14)


def channel_ref() -> str:
    """Канал находок: @имя, ссылка или числовой id."""
    from app.services import secrets

    return (secrets.resolve(KEY_CHANNEL, "") or "").strip()


def parse_channel_ref(raw: str) -> int | str:
    """Привести ссылку на канал к тому, что понимает Telethon.

    Принимаются ``@name``, ``https://t.me/name``, ссылка из веб-клиента
    ``https://web.telegram.org/a/#-1002836789307`` и голый id.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("Канал не задан")

    # Ссылка веб-клиента: идентификатор лежит во фрагменте.
    fragment = re.search(r"#(-?\d+)", value)
    if fragment:
        value = fragment.group(1)

    match = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", value)
    if match:
        return match.group(1)

    if re.fullmatch(r"-?\d+", value):
        number = int(value)
        # -100xxxxxxxxxx — форма id канала в Bot API; MTProto ждёт голый id.
        text = str(abs(number))
        if text.startswith("100") and len(text) > 10:
            return int(text[3:])
        return abs(number)

    return value.lstrip("@")


async def fetch_posts(
    *, limit: int | None = None, detached: bool = False
) -> list[tuple[int, dt.datetime, str]]:
    """Забрать последние сообщения канала.

    Returns:
        Список ``(message_id, когда, текст)``.

    Raises:
        ValueError: канал не задан или недоступен торговому аккаунту.
    """
    from telethon.tl.functions.messages import GetHistoryRequest
    from telethon.tl.types import PeerChannel

    from app.adapters import telegram_gateway

    ref = parse_channel_ref(channel_ref())
    tg = (
        telegram_gateway.detached_gateway()
        if detached
        else telegram_gateway.default_gateway()
    )
    client = await tg.client()

    try:
        entity = await client.get_entity(
            PeerChannel(ref) if isinstance(ref, int) else ref
        )
    except Exception as exc:  # noqa: BLE001 - причина уходит наверх понятным текстом
        raise ValueError(
            f"Канал {channel_ref()!r} недоступен: {type(exc).__name__} {exc}. "
            "Торговый аккаунт должен быть подписан на него."
        ) from exc

    history = await tg.call(
        GetHistoryRequest(
            peer=entity,
            limit=limit or depth(),
            offset_id=0,
            offset_date=None,
            add_offset=0,
            max_id=0,
            min_id=0,
            hash=0,
        )
    )

    out: list[tuple[int, dt.datetime, str]] = []
    for message in getattr(history, "messages", []):
        text = getattr(message, "message", "") or ""
        if not text:
            continue
        when = getattr(message, "date", None)
        out.append((message.id, when, text))
    return out


def depth() -> int:
    """Сколько сообщений читать за проход."""
    from app.services import secrets

    raw = (secrets.resolve(KEY_DEPTH, "") or "").strip()
    try:
        return max(1, min(200, int(raw))) if raw else DEFAULT_DEPTH
    except ValueError:
        return DEFAULT_DEPTH


def store_finds(
    session: Session, message_id: int, posted_at, finds: list[Find]
) -> int:
    """Сохранить находки, не создавая дубликатов.

    Returns:
        Сколько записей добавлено.
    """
    from app.models import FeedFind, utcnow

    added = 0
    for find in finds:
        exists = (
            session.query(FeedFind)
            .filter_by(
                message_id=message_id,
                collection=find.collection,
                number=find.number,
            )
            .first()
        )
        if exists is not None:
            # Пост могли отредактировать: обновляем исход, а не плодим копии.
            exists.realized = find.realized
            exists.value = find.value
            continue

        session.add(
            FeedFind(
                message_id=message_id,
                posted_at=posted_at or utcnow(),
                collection=find.collection,
                number=find.number,
                price=find.price,
                value=find.value,
                currency=find.currency,
                realized=find.realized,
            )
        )
        added += 1
    return added


async def sync(*, detached: bool = False) -> dict:
    """Прочитать канал и сохранить новые находки.

    Args:
        detached: работать с копией сессии в памяти. Так делает панель:
            файл сессии занят воркером, и второй процесс получил бы на
            нём «database is locked».

    Returns:
        Отчёт: сколько постов просмотрено и находок добавлено.
    """
    from app.db import session_scope
    from app.models import utcnow
    from app.services import secrets

    report: dict = {"posts": 0, "finds": 0, "added": 0}

    try:
        posts = await fetch_posts(detached=detached)
    except Exception as exc:  # noqa: BLE001 - канал не должен ронять воркер
        log.warning("Канал находок недоступен: %s", exc)
        return {**report, "error": str(exc)}

    with session_scope() as session:
        lookup = known_collections(session)
        for message_id, posted_at, text in posts:
            if HASHTAG not in text:
                continue
            report["posts"] += 1
            finds = parse_post(text, known=lookup)
            report["finds"] += len(finds)
            report["added"] += store_finds(session, message_id, posted_at, finds)

    secrets.set_value(
        KEY_LAST_SYNC, utcnow().isoformat(timespec="seconds"), actor="auto"
    )
    log.info(
        "Канал находок: постов %s, находок %s, новых %s",
        report["posts"],
        report["finds"],
        report["added"],
    )
    return report


# ----------------------------------------------------------------------
# Ранжирование коллекций
# ----------------------------------------------------------------------
#: Вес находки без подтверждённой продажи. «Оценка» — это прикидка
#: чужого бота, такая же нереализованная, как наша собственная. Считать
#: её наравне с продажей значит верить рекламе вместо сделки.
UNREALIZED_WEIGHT = Decimal("0.3")


@dataclass(slots=True)
class CollectionScore:
    """Насколько коллекция заслуживает внимания сканера."""

    collection: str
    finds: int
    realized: int
    score: Decimal
    median_price: Decimal
    max_price: Decimal

    def as_dict(self) -> dict:
        """Представление для интерфейса."""
        return {
            "collection": self.collection,
            "finds": self.finds,
            "realized": self.realized,
            "score": f"{self.score:.2f}",
            "median_price": f"{self.median_price:.2f}",
            "max_price": f"{self.max_price:.2f}",
        }


def rank_collections(
    session: Session, *, window: dt.timedelta = RANK_WINDOW
) -> list[CollectionScore]:
    """Отсортировать коллекции по тому, что в них реально зарабатывали.

    Вес находки — это её маржа, но подтверждённая продажа считается
    полностью, а чужая оценка — с понижающим коэффициентом.
    """
    from app.models import FeedFind, utcnow

    since = utcnow() - window
    rows = (
        session.query(FeedFind).filter(FeedFind.posted_at >= since).all()
    )

    grouped: dict[str, list[FeedFind]] = {}
    for row in rows:
        grouped.setdefault(row.collection, []).append(row)

    scores: list[CollectionScore] = []
    for collection, items in grouped.items():
        total = Decimal(0)
        realized = 0
        prices: list[Decimal] = []

        for item in items:
            price = Decimal(item.price or 0)
            value = Decimal(item.value or 0)
            if price <= 0:
                continue
            prices.append(price)
            margin = (value / price) - Decimal(1)
            if margin <= 0:
                continue
            if item.realized:
                realized += 1
                total += margin
            else:
                total += margin * UNREALIZED_WEIGHT

        if not prices:
            continue
        prices.sort()
        median = prices[len(prices) // 2]

        scores.append(
            CollectionScore(
                collection=collection,
                finds=len(items),
                realized=realized,
                score=total,
                median_price=median,
                max_price=max(prices),
            )
        )

    scores.sort(key=lambda s: (s.score, s.realized), reverse=True)
    return scores


def top_collections(
    session: Session, *, limit: int = 10, window: dt.timedelta = RANK_WINDOW
) -> list[str]:
    """Названия коллекций для подстановки в сканер."""
    return [s.collection for s in rank_collections(session, window=window)[:limit]]


def last_sync() -> str:
    """Когда канал читали в последний раз."""
    from app.services import secrets

    return secrets.resolve(KEY_LAST_SYNC, "")
