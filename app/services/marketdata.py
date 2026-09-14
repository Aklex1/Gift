"""Рыночные данные: свежесть, робастная медиана, выбросы, ликвидность.

Раздел 6 ТЗ: freshness, history, median/outliers, sample quality,
velocity и confidence.

Раздел 9: неполная история и wash trades дают ложный ROI — поэтому
выборка фильтруется, а её качество явно выражается через Confidence.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.enums import Confidence, Currency, Market
from app.models import FxSnapshot, Listing, MarketFact, utcnow

log = logging.getLogger(__name__)

#: Окно, за которое сделки считаются актуальными.
FRESH_WINDOW = dt.timedelta(days=14)
#: Порог отсечения выбросов в медианных абсолютных отклонениях.
MAD_THRESHOLD = Decimal("3.5")
#: Курс Stars к TON по умолчанию, если снапшота ещё нет.
#: Обновляется реальным снапшотом при первом же скане.
DEFAULT_STARS_PER_TON = Decimal("400")


# ----------------------------------------------------------------------
# Курсы
# ----------------------------------------------------------------------
def latest_fx(session: Session, base: Currency, quote: Currency) -> Decimal | None:
    """Последний известный курс base->quote."""
    if base == quote:
        return Decimal(1)
    row = session.execute(
        select(FxSnapshot)
        .where(FxSnapshot.base == base, FxSnapshot.quote == quote)
        .order_by(FxSnapshot.taken_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return Decimal(row.rate)

    # Пробуем обратный курс.
    inverse = session.execute(
        select(FxSnapshot)
        .where(FxSnapshot.base == quote, FxSnapshot.quote == base)
        .order_by(FxSnapshot.taken_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if inverse is not None and Decimal(inverse.rate) > 0:
        return Decimal(1) / Decimal(inverse.rate)
    return None


def record_fx(
    session: Session, base: Currency, quote: Currency, rate: Decimal, source: str
) -> FxSnapshot:
    """Зафиксировать курс с таймстемпом.

    ТЗ прямо требует timestamped FX snapshot: сравнивать цены разных
    площадок без него нельзя.
    """
    snapshot = FxSnapshot(
        base=base, quote=quote, rate=rate, source=source, taken_at=utcnow()
    )
    session.add(snapshot)
    return snapshot


def to_stars(session: Session, amount: Decimal, currency: Currency) -> Decimal | None:
    """Привести сумму к Stars по последнему снапшоту курса."""
    if currency is Currency.STARS:
        return amount
    rate = latest_fx(session, currency, Currency.STARS)
    if rate is None:
        if currency is Currency.TON:
            log.warning("Нет FX-снапшота TON->STARS, беру значение по умолчанию")
            rate = DEFAULT_STARS_PER_TON
        else:
            return None
    return amount * rate


# ----------------------------------------------------------------------
# Статистика
# ----------------------------------------------------------------------
def robust_median(values: list[Decimal]) -> tuple[Decimal | None, list[Decimal]]:
    """Медиана с отсечением выбросов по MAD.

    Обычная медиана устойчива к единичным аномалиям, но wash trades
    приходят пачками. MAD-фильтр убирает цены, отстоящие от медианы
    больше чем на 3.5 медианных абсолютных отклонения.

    Returns:
        (медиана после очистки, очищенная выборка)
    """
    if not values:
        return (None, [])
    if len(values) < 4:
        return (Decimal(statistics.median(values)), values)

    med = Decimal(statistics.median(values))
    deviations = [abs(v - med) for v in values]
    mad = Decimal(statistics.median(deviations))
    if mad == 0:
        return (med, values)

    cleaned = [v for v in values if abs(v - med) / mad <= MAD_THRESHOLD]
    if not cleaned:
        return (med, values)
    return (Decimal(statistics.median(cleaned)), cleaned)


def assess_confidence(sample_size: int, newest: dt.datetime | None) -> Confidence:
    """Оценить качество выборки.

    Мало сделок или старые данные — торговать по такой оценке нельзя.
    """
    if sample_size == 0 or newest is None:
        return Confidence.NONE
    age = utcnow() - newest
    if sample_size >= 10 and age <= dt.timedelta(days=3):
        return Confidence.HIGH
    if sample_size >= 5 and age <= dt.timedelta(days=7):
        return Confidence.MEDIUM
    if sample_size >= 2 and age <= FRESH_WINDOW:
        return Confidence.LOW
    return Confidence.NONE


CONFIDENCE_ORDER = {
    Confidence.NONE: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def confidence_at_least(actual: Confidence, required: Confidence) -> bool:
    """Не ниже ли фактическое качество выборки требуемого."""
    return CONFIDENCE_ORDER[actual] >= CONFIDENCE_ORDER[required]


# ----------------------------------------------------------------------
# Срез рынка
# ----------------------------------------------------------------------
class MarketSnapshot:
    """Срез рынка по конкретному типу подарка."""

    def __init__(
        self,
        *,
        collection: str,
        model: str | None,
        median_price: Decimal | None,
        floor_price: Decimal | None,
        sample_size: int,
        confidence: Confidence,
        velocity_per_day: float,
        newest: dt.datetime | None,
        active_listings: int,
        source: str = "facts",
    ) -> None:
        self.collection = collection
        self.model = model
        self.median_price = median_price
        self.floor_price = floor_price
        self.sample_size = sample_size
        self.confidence = confidence
        self.velocity_per_day = velocity_per_day
        self.newest = newest
        self.active_listings = active_listings
        self.source = source

    @property
    def days_to_sell(self) -> float | None:
        """Ожидаемый срок продажи при текущей скорости рынка."""
        if self.velocity_per_day <= 0:
            return None
        # Сколько дней уйдёт, чтобы рынок «съел» текущую выкладку.
        return round(max(1.0, self.active_listings / self.velocity_per_day), 1)

    def as_dict(self) -> dict:
        """Срез для сохранения в обосновании решения."""
        return {
            "collection": self.collection,
            "model": self.model,
            "median_price": str(self.median_price) if self.median_price else None,
            "floor_price": str(self.floor_price) if self.floor_price else None,
            "sample_size": self.sample_size,
            "confidence": self.confidence.value,
            "velocity_per_day": self.velocity_per_day,
            "days_to_sell": self.days_to_sell,
            "active_listings": self.active_listings,
            "source": self.source,
        }


def snapshot_for(
    session: Session,
    *,
    collection: str,
    model: str | None = None,
    window: dt.timedelta = FRESH_WINDOW,
) -> MarketSnapshot:
    """Построить срез рынка по накопленным фактам и активным лотам."""
    since = utcnow() - window

    query = session.query(MarketFact).filter(
        MarketFact.collection == collection,
        MarketFact.happened_at >= since,
        MarketFact.suspected_wash.is_(False),
        MarketFact.price_stars.isnot(None),
    )
    if model:
        query = query.filter(MarketFact.model == model)
    facts = query.all()

    prices = [Decimal(f.price_stars) for f in facts if f.price_stars]
    median, cleaned = robust_median(prices)
    newest = max((f.happened_at for f in facts), default=None)

    days = max(1.0, window.total_seconds() / 86400)
    velocity = round(len(cleaned) / days, 3)

    listing_query = session.query(Listing).filter(
        Listing.is_active.is_(True),
        Listing.price_stars.isnot(None),
    )
    listing_query = listing_query.join(Listing.gift).filter_by(collection=collection)
    if model:
        listing_query = listing_query.filter(Listing.gift.has(model=model))
    active = listing_query.all()

    floor = min((Decimal(item.price_stars) for item in active if item.price_stars), default=None)

    return MarketSnapshot(
        collection=collection,
        model=model,
        median_price=median,
        floor_price=floor,
        sample_size=len(cleaned),
        confidence=assess_confidence(len(cleaned), newest),
        velocity_per_day=velocity,
        newest=newest,
        active_listings=len(active),
    )


def snapshot_from_telegram(
    value_info: dict, *, collection: str, model: str | None = None
) -> MarketSnapshot:
    """Построить срез из официальной оценки Telegram.

    ``payments.getUniqueStarGiftValueInfo`` отдаёт floor, среднюю цену и
    последнюю продажу от самого Telegram — это качественнее нашей
    собственной выборки, поэтому такой срез имеет приоритет.
    """
    floor = value_info.get("floor_price")
    average = value_info.get("average_price")
    last_sale = value_info.get("last_sale_price")
    listed = value_info.get("listed_count") or 0

    median = average or last_sale or floor
    last_date = value_info.get("last_sale_date")
    if isinstance(last_date, dt.datetime):
        last_date = last_date.replace(tzinfo=None)
    else:
        last_date = None

    # Данные от самой площадки: доверие высокое, если есть и средняя, и floor.
    if average and floor and last_date:
        confidence = Confidence.HIGH
    elif median and (floor or last_sale):
        confidence = Confidence.MEDIUM
    elif median:
        confidence = Confidence.LOW
    else:
        confidence = Confidence.NONE

    return MarketSnapshot(
        collection=collection,
        model=model,
        median_price=median,
        floor_price=floor,
        sample_size=int(listed or 0),
        confidence=confidence,
        velocity_per_day=0.0,
        newest=last_date,
        active_listings=int(listed or 0),
        source="telegram_value_info",
    )


def record_facts(session: Session, sales: list, market: Market) -> int:
    """Сохранить историю продаж, отфильтровав дубликаты и wash trades."""
    saved = 0
    for sale in sales:
        if not sale.external_id:
            continue
        exists = (
            session.query(MarketFact)
            .filter_by(market=market, external_id=sale.external_id)
            .first()
        )
        if exists:
            continue
        # Продавец равен покупателю — типичный признак накрутки объёма.
        wash = bool(sale.buyer and sale.seller and sale.buyer == sale.seller)
        price_stars = to_stars(session, sale.price, sale.currency)
        session.add(
            MarketFact(
                market=market,
                collection=sale.gift.collection,
                model=sale.gift.model,
                backdrop=sale.gift.backdrop,
                symbol=sale.gift.symbol,
                price=sale.price,
                currency=sale.currency,
                price_stars=price_stars,
                happened_at=sale.happened_at,
                suspected_wash=wash,
                external_id=sale.external_id,
                raw=sale.raw,
            )
        )
        saved += 1
    return saved
