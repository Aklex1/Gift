"""Сравнение цен на одну и ту же модель между площадками.

Один и тот же подарок на разных площадках стоит по-разному, и разница
доходит до десятков процентов: Light Sword / Enforcer в один момент
стоил 7.20 на Portals, 7.42 и 8.79 на MRKT, 7.71 на Tonnel — размах
22% на одной модели.

Это и есть механика, которой не нужен никакой прогноз: обе цены названы
рынком, и ошибиться можно только в комиссиях и в переносе. Но именно в
них и ошибаются: «купить где дешевле, продать где дороже» — неверное
правило. Самая дорогая площадка обычно та, у которой выше комиссия с
продажи: Telegram удерживает 20%, площадки на TON — единицы процентов,
и заявки это уже учитывают. Разница цен в 18% в сторону Telegram — это
его комиссия, а не находка: после неё остаётся минус 5%.

Поэтому направление здесь выбирается по остатку после комиссий и
переноса, а размах цен показывается рядом как справка.

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


def net_after_fees(
    session: Session, *, buy_market: Market, buy: Decimal,
    sell_market: Market, sell: Decimal,
) -> Decimal | None:
    """Что останется от разницы после комиссий и переноса.

    Без этого таблица цен читается неверно, и это не гипотеза: у
    Telegram комиссия с продажи 20%, поэтому его заявки стоят примерно
    на столько же выше, чем на площадках TON. Разница в цене 18%
    выглядит находкой, а после комиссии оборачивается убытком в 5%.
    """
    from app.services import arbitrage, valuation

    buy_fees = valuation.fees_in(
        session, valuation.get_fees(session, buy_market), Currency.STARS
    )
    sell_fees = valuation.fees_in(
        session, valuation.get_fees(session, sell_market), Currency.STARS
    )
    transfer = marketdata.to_stars(
        session, arbitrage.transfer_cost_ton(), Currency.TON
    ) or Decimal(0)

    cost = valuation.total_cost_of(buy, buy_fees) + transfer
    if cost <= 0:
        return None
    proceeds = valuation.net_proceeds_from(sell, sell_fees)
    return (proceeds - cost) / cost


def sale_fee_rates(session: Session, markets: list[Market]) -> dict[Market, Decimal]:
    """Сколько площадка удерживает с продажи.

    Нужна не для расчёта, а чтобы таблицу можно было прочитать: пока
    комиссия не названа, разница цен выглядит прибылью.
    """
    from app.services import valuation

    return {
        market: valuation.get_fees(session, market).total_sale_rate
        for market in markets
    }


def best_direction(
    session: Session, priced: dict[Market, Quote]
) -> tuple[Market, Quote, Market, Quote, Decimal] | None:
    """Пара площадок, на которой остаётся больше всего после комиссий.

    Не «где дешевле» и «где дороже». Самая дорогая площадка почти
    всегда та, у которой выше комиссия с продажи: Telegram удерживает
    20%, площадки на TON — единицы процентов, и заявки это учитывают.
    Поэтому пара с наибольшей разницей цен и пара, на которой остаются
    деньги, — это, как правило, разные пары.

    Returns:
        ``(площадка покупки, лот, площадка продажи, лот, остаток)`` или
        ``None``, если ни одна пара не считается.
    """
    best: tuple[Market, Quote, Market, Quote, Decimal] | None = None
    for buy_market, buy in priced.items():
        for sell_market, sell in priced.items():
            if buy_market is sell_market:
                continue
            if sell.price_stars <= buy.price_stars:
                continue
            net = net_after_fees(
                session,
                buy_market=buy_market, buy=buy.price_stars,
                sell_market=sell_market, sell=sell.price_stars,
            )
            if net is None:
                continue
            if best is None or net > best[4]:
                best = (buy_market, buy, sell_market, sell, net)
    return best


def spreads(
    table: dict[str, dict[Market, Quote]], session: Session | None = None
) -> list[dict]:
    """Где одну и ту же модель выгодно перекладывать между площадками.

    Сравнение идёт в Stars: площадки номинируют цены в разных валютах,
    и сравнивать GRAM с Stars напрямую значило бы выдать курс за
    разницу цен.

    Модели, которые нашлись лишь на одной площадке, в сводку не идут —
    сравнивать не с чем, а не «разницы нет».

    Args:
        session: с ней направление выбирается по остатку после
            комиссий и переноса, а не по размаху цен. Без неё берутся
            самая дешёвая и самая дорогая площадка — это видно как
            разница, но читать её как прибыль нельзя.
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

        chosen = best_direction(session, priced) if session is not None else None
        if chosen is not None:
            buy_market, buy, sell_market, sell, net = chosen
        else:
            # Либо считаем без сессии, либо все цены равны и
            # направления нет. Показываем размах — но без остатка.
            buy_market, buy = min(priced.items(), key=lambda kv: kv[1].price_stars)
            sell_market, sell = max(priced.items(), key=lambda kv: kv[1].price_stars)
            net = None

        out.append(
            {
                "model": model,
                "buy_market": buy_market,
                "buy": buy,
                "sell_market": sell_market,
                "sell": sell,
                "gap": (sell.price_stars / buy.price_stars) - Decimal(1),
                "net_roi": net,
                "venues": len(priced),
            }
        )

    # С комиссиями сортируем по тому, что остаётся: строка с размахом
    # 18% и убытком 5% не должна стоять выше прибыльной с размахом 6%.
    if session is not None:
        out.sort(
            key=lambda row: (
                row["net_roi"] if row["net_roi"] is not None else Decimal(-99)
            ),
            reverse=True,
        )
    else:
        out.sort(key=lambda row: row["gap"], reverse=True)
    return out
