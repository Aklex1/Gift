"""Поиск разницы цен между площадками.

Один и тот же подарок торгуется на нескольких площадках, и цены там
расходятся: ликвидность разная, аудитория разная, комиссии разные.
Здесь ищется разница, которая переживёт обе комиссии, перенос NFT и
курс — то есть та, на которой действительно можно заработать.

Чего здесь сознательно нет — обещания лёгких денег. Сделка «купить на
A, продать на B» не атомарна: между покупкой и продажей подарок нужно
перевести, а это минуты, сетевая комиссия и риск, что за это время
цена на B уйдёт. Поэтому:

* за ориентир продажи берётся **самая дешёвая** активная цена на
  целевой площадке, а не медиана и не максимум: чтобы продать быстро,
  придётся встать не дороже текущего минимума;
* из выручки вычитается стоимость переноса;
* риск сделки повышается на фиксированную величину — ровно потому,
  что гарантий исполнения второй ноги нет.

Сравнение идёт на уровне «коллекция + модель»: конкретный NFT в один
момент времени продаётся только на одной площадке, поэтому сравнивать
его сам с собой бессмысленно. А вот «Plush Pepe с моделью Neon» на
двух площадках — это сопоставимый товар.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import ListingDTO
from app.enums import Currency, Market
from app.services import marketdata, store, valuation

log = logging.getLogger(__name__)

#: Минимальная чистая доходность связки, ниже которой не показываем.
DEFAULT_MIN_ROI = Decimal("0.15")

#: Стоимость переноса NFT между площадками, в TON. Это сетевые
#: комиссии TON за transfer: сам перенос бесплатным не бывает.
DEFAULT_TRANSFER_TON = Decimal("0.1")

#: Надбавка к риску за неатомарность второй ноги.
TRANSFER_RISK = 22

#: Сколько минут цены на другой площадке считаются сопоставимыми.
#: Данные одного прохода сканера, дальше сравнение теряет смысл.
KEY_MIN_ROI = "ARB_MIN_ROI"
KEY_TRANSFER_TON = "ARB_TRANSFER_TON"
KEY_ENABLED = "ARB_ENABLED"


def enabled() -> bool:
    """Включён ли поиск межплощадочной разницы.

    По умолчанию выключен: связка требует ручного переноса подарка,
    и включать её без ведома владельца нельзя.
    """
    return (store.get(KEY_ENABLED) or "").lower() in ("1", "true", "on", "yes")


def set_enabled(value: bool) -> None:
    """Включить или выключить поиск разницы цен."""
    store.set(KEY_ENABLED, "1" if value else "0")


def min_roi() -> Decimal:
    """Порог доходности связки."""
    raw = store.get(KEY_MIN_ROI)
    try:
        return Decimal(raw) if raw else DEFAULT_MIN_ROI
    except Exception:  # noqa: BLE001
        return DEFAULT_MIN_ROI


def transfer_cost_ton() -> Decimal:
    """Во что обходится перенос подарка между площадками."""
    raw = store.get(KEY_TRANSFER_TON)
    try:
        return Decimal(raw) if raw else DEFAULT_TRANSFER_TON
    except Exception:  # noqa: BLE001
        return DEFAULT_TRANSFER_TON


def kind_key(dto: ListingDTO) -> str | None:
    """Ключ сопоставимого товара: коллекция и модель.

    ``None`` — модель неизвестна, и сравнивать нечего: цена подарка
    внутри коллекции различается в разы именно из-за модели.
    """
    collection = (dto.gift.collection or "").strip().lower()
    model = (dto.gift.model or "").strip().lower()
    if not collection or not model:
        return None
    return f"{collection}|{model}"


@dataclass(slots=True)
class MarketQuote:
    """Самое дешёвое предложение одной площадки по одному товару."""

    market: Market
    price_stars: Decimal
    price_native: Decimal
    currency: Currency
    external_id: str
    listings: int = 1


@dataclass(slots=True)
class Spread:
    """Найденная разница цен между двумя площадками."""

    kind: str
    buy: MarketQuote
    sell: MarketQuote
    total_cost: Decimal
    net_proceeds: Decimal
    net_profit: Decimal
    net_roi: Decimal
    risk_bonus: int = TRANSFER_RISK
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Представление для интерфейса и журнала."""
        return {
            "kind": self.kind,
            "buy_market": self.buy.market.value,
            "buy_price_stars": str(self.buy.price_stars.quantize(Decimal("1"))),
            "buy_price_native": f"{self.buy.price_native} {self.buy.currency.value}",
            "sell_market": self.sell.market.value,
            "sell_price_stars": str(self.sell.price_stars.quantize(Decimal("1"))),
            "net_profit_stars": str(self.net_profit.quantize(Decimal("1"))),
            "net_roi": f"{self.net_roi:.1%}",
            "sell_side_listings": self.sell.listings,
            "reasons": self.reasons,
        }


def build_index(
    session: Session, listings: list[ListingDTO]
) -> dict[str, dict[Market, MarketQuote]]:
    """Собрать самые дешёвые цены по товарам и площадкам."""
    index: dict[str, dict[Market, MarketQuote]] = {}

    for dto in listings:
        key = kind_key(dto)
        if key is None:
            continue
        price_stars = marketdata.to_stars(session, dto.price, dto.currency)
        if price_stars is None or price_stars <= 0:
            continue

        per_market = index.setdefault(key, {})
        current = per_market.get(dto.market)
        if current is None:
            per_market[dto.market] = MarketQuote(
                market=dto.market,
                price_stars=price_stars,
                price_native=dto.price,
                currency=dto.currency,
                external_id=dto.external_id,
            )
            continue

        current.listings += 1
        if price_stars < current.price_stars:
            current.price_stars = price_stars
            current.price_native = dto.price
            current.currency = dto.currency
            current.external_id = dto.external_id

    return index


def evaluate_pair(
    session: Session, kind: str, buy: MarketQuote, sell: MarketQuote
) -> Spread | None:
    """Посчитать связку «купить на buy — продать на sell».

    Returns:
        ``None``, если после всех издержек связка убыточна.
    """
    # Весь расчёт идёт в Stars, поэтому и сетевые комиссии площадок
    # приводим к Stars: у Portals и MRKT они заданы в TON.
    buy_fees = valuation.fees_in(
        session, valuation.get_fees(session, buy.market), Currency.STARS
    )
    sell_fees = valuation.fees_in(
        session, valuation.get_fees(session, sell.market), Currency.STARS
    )

    # Чтобы продать быстро, встаём под текущий минимум целевой площадки.
    # Считаем ровно по минимуму, без скидки: так оценка остаётся
    # проверяемой, а запас прочности даёт порог доходности.
    expected_sale = sell.price_stars

    transfer_stars = marketdata.to_stars(
        session, transfer_cost_ton(), Currency.TON
    ) or Decimal(0)

    total_cost = (
        valuation.total_cost_of(buy.price_stars, buy_fees) + transfer_stars
    )
    net_proceeds = valuation.net_proceeds_from(expected_sale, sell_fees)
    net_profit = net_proceeds - total_cost
    if total_cost <= 0 or net_profit <= 0:
        return None

    net_roi = net_profit / total_cost

    reasons = [
        f"покупка на {buy.market.value}: {buy.price_native} "
        f"{buy.currency.value} ({buy.price_stars:.0f} Stars)",
        f"продажа на {sell.market.value} по текущему минимуму "
        f"{sell.price_stars:.0f} Stars, на руки {net_proceeds:.0f} "
        f"(комиссия {sell_fees.total_sale_rate:.0%})",
        f"перенос подарка: {transfer_cost_ton()} TON "
        f"({transfer_stars:.0f} Stars)",
        "сделка не атомарна: между покупкой и продажей подарок нужно "
        f"перевести, риск +{TRANSFER_RISK}",
    ]
    if sell.listings < 3:
        reasons.append(
            f"на {sell.market.value} всего {sell.listings} лот(а) этого вида: "
            "цена там может быть случайной"
        )

    return Spread(
        kind=kind,
        buy=buy,
        sell=sell,
        total_cost=total_cost,
        net_proceeds=net_proceeds,
        net_profit=net_profit,
        net_roi=net_roi,
        reasons=reasons,
    )


def find(
    session: Session,
    listings: list[ListingDTO],
    *,
    threshold: Decimal | None = None,
    markets: set[Market] | None = None,
) -> list[Spread]:
    """Найти все связки, которые переживают комиссии и перенос.

    Args:
        listings: лоты одного прохода сканера — цены сопоставимы
            только внутри прохода.
        markets: чем ограничить площадки; по умолчанию все.

    Returns:
        Связки по убыванию доходности.
    """
    limit = min_roi() if threshold is None else threshold
    index = build_index(session, listings)
    found: list[Spread] = []

    for kind, quotes in index.items():
        usable = {
            market: quote
            for market, quote in quotes.items()
            if markets is None or market in markets
        }
        if len(usable) < 2:
            continue

        for buy_market, buy in usable.items():
            for sell_market, sell in usable.items():
                if buy_market is sell_market:
                    continue
                # Продавать дешевле, чем купили, смысла нет — это
                # отсекает половину пар сразу и экономит расчёт.
                if sell.price_stars <= buy.price_stars:
                    continue
                spread = evaluate_pair(session, kind, buy, sell)
                if spread is not None and spread.net_roi >= limit:
                    found.append(spread)

    found.sort(key=lambda s: s.net_roi, reverse=True)
    return found
