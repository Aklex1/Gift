"""Сравнение цен на одну и ту же модель между площадками.

Один и тот же подарок на разных площадках стоит по-разному, и разница
доходит до десятков процентов: Light Sword / Enforcer в один момент
стоил 7.20 на Portals, 7.42 и 8.79 на MRKT, 7.71 на Tonnel — размах
22% на одной модели.

Это и есть механика, которой не нужен никакой прогноз: купить там, где
дешевле, выставить там, где дороже. В отличие от оценки «сколько это
стоит на самом деле», здесь обе цены названы рынком, и ошибиться можно
только в комиссиях и в переносе.

Здесь — измерительный прибор, а не торговля. Он показывает картину до
того, как подключать площадку к сканеру: стоит ли она запросов.
Решения по связкам принимает app.services.arbitrage, который сводит
лоты одного прохода; этот модуль ходит за ценами сам.

Один запрос на площадку: берётся страница самых дешёвых лотов
коллекции, и цены раскладываются по моделям. Спрашивать каждую модель
отдельно значило бы десятки запросов ради той же таблицы.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import Capability, SearchSkipped
from app.adapters.registry import get_adapter, tradable_markets
from app.enums import Currency, Market
from app.services import marketdata

log = logging.getLogger(__name__)

#: Сколько лотов смотрим на площадке. Сотня самых дешёвых покрывает
#: floor по всем ходовым моделям коллекции.
PAGE = 100


class Quote:
    """Самый дешёвый лот одной модели на одной площадке."""

    def __init__(
        self,
        *,
        market: Market,
        price: Decimal,
        currency: Currency,
        price_stars: Decimal | None,
        external_id: str,
    ) -> None:
        self.market = market
        self.price = price
        self.currency = currency
        self.price_stars = price_stars
        self.external_id = external_id


async def quotes_for(
    session: Session, collection: str, *, markets: list[Market] | None = None
) -> tuple[dict[str, dict[Market, Quote]], list[str]]:
    """Цены по моделям и площадкам для одной коллекции.

    Returns:
        (модель -> площадка -> самый дешёвый лот, замечания по площадкам)
    """
    wanted = markets or tradable_markets()
    table: dict[str, dict[Market, Quote]] = {}
    notes: list[str] = []

    for market in wanted:
        adapter = get_adapter(market)
        if not adapter.supports(Capability.SEARCH):
            notes.append(f"{market.value}: поиск недоступен (нет токена?)")
            continue
        try:
            rows = await adapter.search(collection=collection, limit=PAGE)
        except SearchSkipped as exc:
            notes.append(f"{market.value}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - одна площадка не рушит сводку
            notes.append(f"{market.value}: {type(exc).__name__}: {exc}")
            continue

        if not rows:
            notes.append(f"{market.value}: лотов не вернулось")
            continue

        for dto in rows:
            model = (dto.gift.model or "").strip()
            if not model:
                # Без модели сравнивать нечего: цена внутри коллекции
                # различается в разы именно из-за неё.
                continue
            current = table.setdefault(model, {}).get(market)
            if current is not None and current.price <= dto.price:
                continue
            table[model][market] = Quote(
                market=market,
                price=dto.price,
                currency=dto.currency,
                price_stars=marketdata.to_stars(session, dto.price, dto.currency),
                external_id=dto.external_id,
            )
    return table, notes


def spreads(table: dict[str, dict[Market, Quote]]) -> list[dict]:
    """Где одна и та же модель стоит по-разному.

    Сравнение идёт в Stars: площадки номинируют цены в разных валютах,
    и сравнивать GRAM с Stars напрямую значило бы выдать курс за
    разницу цен.

    Модели, которые нашлись лишь на одной площадке, в сводку не идут —
    сравнивать не с чем, а не «разницы нет».
    """
    out: list[dict] = []
    for model, per_market in table.items():
        priced = {
            market: quote
            for market, quote in per_market.items()
            if quote.price_stars and quote.price_stars > 0
        }
        if len(priced) < 2:
            continue

        cheap_market, cheap = min(priced.items(), key=lambda kv: kv[1].price_stars)
        dear_market, dear = max(priced.items(), key=lambda kv: kv[1].price_stars)
        out.append(
            {
                "model": model,
                "buy_market": cheap_market,
                "buy": cheap,
                "sell_market": dear_market,
                "sell": dear,
                "gap": (dear.price_stars / cheap.price_stars) - Decimal(1),
                "venues": len(priced),
            }
        )

    out.sort(key=lambda row: row["gap"], reverse=True)
    return out
