"""Статистика состоявшихся сделок: разброс цен и скорость.

Здесь живут две величины, ради которых подключался Fragment.

**Разброс** — во сколько раз медианная сделка дороже дешёвого хвоста.
Он отвечает на вопрос, который нельзя задать floor'у: бывают ли в этой
коллекции дешёвые входы вообще. Там, где все сделки укладываются в
узкую полосу, недооценённому лоту взяться неоткуда, сколько его ни
карауль; там, где медиана вдвое выше десятого процентиля, дешёвые
покупки случаются регулярно — это и есть угодья.

**Скорость** — сколько таких подарков уходит в день. Без неё выгодная
на бумаге сделка может оказаться деньгами, замороженными на месяцы.

Обе считаются **в пределах одной площадки**. Это не педантизм: один и
тот же подарок на Fragment и в Telegram стоит по-разному, и сведённая
по обеим выборка показала бы разброс там, где есть только разница
площадок. Такой «разброс» выглядел бы как возможность и ею не был.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.enums import Market
from app.models import MarketFact, utcnow

#: За какой срок берём сделки. Месяц — компромисс: короче окно даёт
#: слишком мало сделок по редким моделям, длиннее тянет за собой цены,
#: которых на рынке уже нет.
WINDOW = dt.timedelta(days=30)

#: Меньше этого числа сделок — не выборка, а совпадение. Разброс по
#: трём точкам управляется одной случайной дешёвой продажей.
MIN_SALES = 5

#: Площадка, с которой берётся история. Единственная, показывающая
#: чужие сделки, а не только наши.
SOURCE_MARKET = Market.FRAGMENT


@dataclass(frozen=True)
class SaleStats:
    """Что известно о сделках по этому виду подарков."""

    sales: int
    low: Decimal | None
    median: Decimal | None
    high: Decimal | None
    velocity_per_day: float
    newest: dt.datetime | None

    @property
    def spread(self) -> float | None:
        """Во сколько раз медиана выше дешёвого хвоста.

        None, когда сделок мало: разброс по единичным точкам — это
        шум, а выданный за показатель шум хуже его отсутствия.
        """
        if self.sales < MIN_SALES or not self.low or not self.median:
            return None
        if self.low <= 0:
            return None
        return round(float(self.median / self.low), 2)

    @property
    def reliable(self) -> bool:
        """Достаточно ли сделок, чтобы на числа опираться."""
        return self.sales >= MIN_SALES

    def as_dict(self) -> dict:
        """Сводка для обоснования кандидата."""
        return {
            "sales": self.sales,
            "low": str(self.low.quantize(Decimal("1"))) if self.low else None,
            "median": str(self.median.quantize(Decimal("1"))) if self.median else None,
            "high": str(self.high.quantize(Decimal("1"))) if self.high else None,
            "velocity_per_day": self.velocity_per_day,
            "spread": self.spread,
            "source": SOURCE_MARKET.value,
        }


EMPTY = SaleStats(
    sales=0, low=None, median=None, high=None, velocity_per_day=0.0, newest=None
)


def percentile(values: list[Decimal], share: float) -> Decimal | None:
    """Значение, ниже которого лежит заданная доля выборки.

    Без интерполяции: берётся ближайший реально состоявшийся платёж.
    Интерполированная цена — это цена, по которой никто не покупал, а
    нам важно опираться на то, что случилось.
    """
    if not values:
        return None
    import math

    ordered = sorted(values)
    # Метод ближайшего ранга: первое значение, ниже которого лежит
    # заданная доля выборки. Через round() было бы короче, но round()
    # в Python округляет половину к чётному, и процентиль прыгал бы на
    # соседнюю сделку в зависимости от размера выборки.
    index = math.ceil(share * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def sale_stats(
    session: Session,
    *,
    collection: str,
    model: str | None = None,
    window: dt.timedelta = WINDOW,
    market: Market = SOURCE_MARKET,
) -> SaleStats:
    """Собрать статистику сделок по виду подарка.

    Args:
        model: если задана — считаем по ней. Разброс внутри коллекции
            почти весь объясняется разницей моделей, и смешивать их
            значило бы принять дорогую модель за дешёвый хвост.
    """
    since = utcnow() - window
    query = session.query(MarketFact).filter(
        MarketFact.market == market,
        MarketFact.collection == collection,
        MarketFact.happened_at >= since,
        MarketFact.suspected_wash.is_(False),
        MarketFact.price_stars.isnot(None),
    )
    if model:
        query = query.filter(MarketFact.model == model)
    facts = query.all()
    if not facts:
        return EMPTY

    prices = [Decimal(fact.price_stars) for fact in facts if fact.price_stars]
    if not prices:
        return EMPTY

    days = max(window.total_seconds() / 86400, 1.0)
    return SaleStats(
        sales=len(prices),
        low=percentile(prices, 0.10),
        median=percentile(prices, 0.50),
        high=percentile(prices, 0.90),
        velocity_per_day=round(len(prices) / days, 3),
        newest=max((fact.happened_at for fact in facts), default=None),
    )


def hunting_grounds(
    session: Session, *, window: dt.timedelta = WINDOW, limit: int = 20
) -> list[dict]:
    """Где дешёвые входы случаются чаще всего.

    Ранжирование по «разброс × скорость»: широкий разброс без сделок —
    мёртвая коллекция, а бойкая торговля в узкой полосе цен дешёвых
    входов не даёт. Нужны обе половины сразу.

    Разброс считается и по модели, и по коллекции целиком, но значат
    они **разное**, и путать их нельзя:

    * по модели — разброс цен на одно и то же. Это и есть возможность:
      такой же подарок кто-то отдаёт дешевле;
    * по коллекции — в основном разница между моделями. Рядовой
      экземпляр стоит 4 GRAM, редкий 16, и отношение 4.0 говорит не о
      скидках, а о премии за редкость. Купить рядовой по 4 и продать
      как редкий нельзя.

    Второе всё же полезно, но для другого: там, где модели различаются
    в разы, редкая модель может появиться по цене рядовой — и вот это
    уже находка. Поэтому строки помечаются, а не смешиваются.

    Модельные идут первыми: они говорят о прибыли напрямую.
    """
    since = utcnow() - window
    rows = (
        session.query(MarketFact.collection, MarketFact.model)
        .filter(
            MarketFact.market == SOURCE_MARKET,
            MarketFact.happened_at >= since,
            MarketFact.suspected_wash.is_(False),
            MarketFact.price_stars.isnot(None),
        )
        .distinct()
        .all()
    )

    out: list[dict] = []
    for collection, model in rows:
        stats = sale_stats(
            session, collection=collection, model=model, window=window
        )
        spread = stats.spread
        if spread is None:
            continue
        out.append(
            {
                "collection": collection,
                "model": model,
                # Чем меряли: одним и тем же подарком или коллекцией,
                # где разброс — это в основном разница моделей.
                "scope": "модель" if model else "коллекция",
                "spread": spread,
                "velocity_per_day": stats.velocity_per_day,
                "sales": stats.sales,
                "low": stats.low,
                "median": stats.median,
                # Обе половины сразу: и насколько широка полоса цен, и
                # как часто по ней вообще торгуют.
                "score": round(spread * stats.velocity_per_day, 3),
            }
        )

    # Сначала модельные: они про прибыль, а коллекционные — про то,
    # где её стоит караулить.
    out.sort(key=lambda row: (row["model"] is None, -row["score"]))
    return out[:limit]
