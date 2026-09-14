"""Оценка сделки: ROI с учётом комиссий, риск и объяснимое решение.

Раздел 6 ТЗ: fee-aware ROI, liquidity, risk 0-100, explainable decision.

Главное правило расчёта: прибыль считается от суммы, которая реально
дойдёт до нас после всех комиссий площадки и роялти — иначе «прибыльная»
сделка оказывается убыточной.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.orm import Session

from app.enums import Confidence, Currency, Market
from app.models import FeeSchedule
from app.services.marketdata import MarketSnapshot

log = logging.getLogger(__name__)

#: Комиссии по умолчанию, если в БД нет актуальной версии.
#:
#: Это оценка, а не факт: площадки меняют тарифы и проводят акции.
#: После первой реальной продажи сверьте фактическое зачисление и
#: заведите новую версию в таблице fee_schedules — иначе весь расчёт
#: ROI будет смещён (см. docs/OPERATIONS.md).
#:
#: Сетевая комиссия указана в валюте площадки: у Portals, MRKT, Tonnel
#: и Getgems это TON, а не Stars. Считать её как Stars означало бы
#: занизить издержки в несколько десятков раз, поэтому `get_fees`
#: приводит её к Stars по актуальному курсу.
DEFAULT_FEES: dict[Market, dict[str, Decimal]] = {
    Market.TELEGRAM: {
        "sale_fee": Decimal("0.20"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0"),
        "currency": Currency.STARS,
    },
    # Portals берёт около 2,5% с продавца — заметно меньше Telegram.
    # Площадка периодически объявляет нулевую комиссию, поэтому
    # значение стоит сверить по первой же реальной продаже.
    Market.PORTALS: {
        "sale_fee": Decimal("0.025"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.05"),
        "currency": Currency.TON
    },
    Market.MRKT: {
        "sale_fee": Decimal("0.05"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.1"),
        "currency": Currency.TON
    },
    Market.TONNEL: {
        "sale_fee": Decimal("0.06"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.1"),
        "currency": Currency.TON
    },
    Market.GETGEMS: {
        "sale_fee": Decimal("0.05"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0.05"),
        "network_fee": Decimal("0.1"),
        "currency": Currency.TON
    },
}


#: Шаг цены по валютам. Stars целые, TON торгуется с сотыми:
#: округление до целого раздувало цену лота почти на четверть.
PRICE_STEP: dict[Currency, Decimal] = {
    Currency.STARS: Decimal("1"),
    Currency.TON: Decimal("0.01"),
    Currency.USD: Decimal("0.01"),
    Currency.RUB: Decimal("0.01"),
}


def round_price(value: Decimal, currency: Currency) -> Decimal:
    """Округлить цену к шагу, принятому на площадке.

    Округление вниз: лучше выставить чуть дешевле и продать, чем
    случайно задрать цену выше рынка.
    """
    step = PRICE_STEP.get(currency, Decimal("0.01"))
    return (Decimal(value) / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(slots=True)
class Fees:
    """Комиссии конкретной площадки."""

    sale_fee: Decimal
    buy_fee: Decimal
    royalty: Decimal
    network_fee: Decimal
    currency: Currency = Currency.STARS
    version: int = 0

    @property
    def total_sale_rate(self) -> Decimal:
        """Суммарная доля, удерживаемая при продаже."""
        return self.sale_fee + self.royalty


def get_fees(session: Session, market: Market) -> Fees:
    """Актуальная версия комиссий площадки."""
    row = (
        session.query(FeeSchedule)
        .filter_by(market=market, is_active=True)
        .order_by(FeeSchedule.version.desc())
        .first()
    )
    if row is not None:
        return Fees(
            sale_fee=Decimal(row.sale_fee),
            buy_fee=Decimal(row.buy_fee),
            royalty=Decimal(row.royalty),
            network_fee=Decimal(row.network_fee),
            currency=row.currency,
            version=row.version,
        )
    defaults = DEFAULT_FEES.get(market, DEFAULT_FEES[Market.TELEGRAM])
    return Fees(**defaults)  # type: ignore[arg-type]


def fees_in(session: Session, fees: Fees, currency: Currency) -> Fees:
    """Выразить комиссии в нужной валюте.

    Доли (sale_fee, royalty, buy_fee) от валюты не зависят, а
    network_fee — это сумма. У площадок на TON она задана в TON: если
    считать её Stars, издержки занижаются в десятки раз, а если
    считать сумму в Stars как TON — завышаются во столько же. Поэтому
    каждый расчёт говорит, в какой валюте он идёт.
    """
    if fees.currency is currency or not fees.network_fee:
        return replace(fees, currency=currency)

    from app.services import marketdata

    in_stars = marketdata.to_stars(session, fees.network_fee, fees.currency)
    if in_stars is None:
        # Курса нет — честнее оставить как есть, чем выдумать число.
        return fees
    if currency is Currency.STARS:
        return replace(fees, network_fee=in_stars, currency=Currency.STARS)

    rate = marketdata.to_stars(session, Decimal(1), currency)
    if not rate:
        return fees
    return replace(fees, network_fee=in_stars / rate, currency=currency)


def seed_fee_schedules(session: Session) -> None:
    """Записать версии комиссий по умолчанию, если их ещё нет."""
    for market, values in DEFAULT_FEES.items():
        exists = session.query(FeeSchedule).filter_by(market=market).first()
        if exists:
            continue
        session.add(
            FeeSchedule(
                market=market,
                version=1,
                sale_fee=values["sale_fee"],
                buy_fee=values["buy_fee"],
                royalty=values["royalty"],
                network_fee=values["network_fee"],
                currency=values.get("currency", Currency.STARS),
                is_active=True,
                note="значения по умолчанию; уточните по фактическим сделкам",
            )
        )


@dataclass(slots=True)
class Valuation:
    """Результат оценки сделки — объяснимое решение."""

    buy_price: Decimal
    fair_value: Decimal
    expected_sale_price: Decimal
    net_proceeds: Decimal
    total_cost: Decimal
    net_profit: Decimal
    net_roi: Decimal
    risk_score: int
    confidence: Confidence
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def is_profitable(self) -> bool:
        """Положительна ли чистая прибыль."""
        return self.net_profit > 0

    def as_dict(self) -> dict:
        """Обоснование для сохранения в намерении и показа владельцу."""
        return {
            "buy_price": str(self.buy_price),
            "fair_value": str(self.fair_value),
            "expected_sale_price": str(self.expected_sale_price),
            "net_proceeds": str(self.net_proceeds),
            "total_cost": str(self.total_cost),
            "net_profit": str(self.net_profit),
            "net_roi": str(self.net_roi),
            "risk_score": self.risk_score,
            "confidence": self.confidence.value,
            "reasons": self.reasons,
            "blockers": self.blockers,
        }


def net_proceeds_from(price: Decimal, fees: Fees) -> Decimal:
    """Сколько реально дойдёт до нас после продажи по цене ``price``."""
    return price * (Decimal(1) - fees.total_sale_rate) - fees.network_fee


def total_cost_of(price: Decimal, fees: Fees) -> Decimal:
    """Полная себестоимость покупки по цене ``price``."""
    return price * (Decimal(1) + fees.buy_fee) + fees.network_fee


def compute_risk(
    *,
    snapshot: MarketSnapshot,
    buy_price: Decimal,
    fair_value: Decimal,
    market: Market,
    is_official_api: bool,
) -> tuple[int, list[str]]:
    """Риск сделки 0..100 с объяснением каждого слагаемого.

    0 — минимальный риск, 100 — торговать нельзя.
    """
    score = 0
    reasons: list[str] = []

    # 1. Качество данных — главный источник ложного ROI.
    data_penalty = {
        Confidence.HIGH: 0,
        Confidence.MEDIUM: 12,
        Confidence.LOW: 28,
        Confidence.NONE: 55,
    }[snapshot.confidence]
    score += data_penalty
    if data_penalty:
        reasons.append(
            f"качество данных {snapshot.confidence.value}: +{data_penalty} "
            f"(выборка {snapshot.sample_size})"
        )

    # 2. Ликвидность: нечего продавать быстро — деньги замораживаются.
    days = snapshot.days_to_sell
    if days is None:
        score += 20
        reasons.append("скорость продаж неизвестна: +20")
    elif days > 30:
        score += 18
        reasons.append(f"ожидаемый срок продажи {days} дн.: +18")
    elif days > 14:
        score += 10
        reasons.append(f"ожидаемый срок продажи {days} дн.: +10")

    # 3. Толщина рынка: единичные лоты легко двигаются манипуляцией.
    if snapshot.active_listings < 3:
        score += 12
        reasons.append(f"мало активных лотов ({snapshot.active_listings}): +12")

    # 4. Приватный API без SLA — риск блокировки и потери средств.
    if not is_official_api:
        score += 25
        reasons.append(f"{market.value}: приватный API без SLA: +25")

    # 5. Слишком хорошая скидка — чаще ошибка данных, чем удача.
    if fair_value > 0:
        discount = (fair_value - buy_price) / fair_value
        if discount > Decimal("0.6"):
            score += 15
            reasons.append(
                f"подозрительно большая скидка {discount:.0%}: +15 "
                "(вероятна ошибка оценки)"
            )

    return (max(0, min(100, score)), reasons)


def evaluate(
    session: Session,
    *,
    buy_market: Market,
    buy_price: Decimal,
    sell_market: Market,
    snapshot: MarketSnapshot,
    is_official_api: bool,
    target_markup: Decimal = Decimal("0"),
) -> Valuation:
    """Оценить сделку «купить на A — продать на B».

    Args:
        buy_price: цена покупки в Stars.
        snapshot: срез рынка для этого типа подарка.
        target_markup: желаемая наценка сверх справедливой цены.

    Returns:
        Полная оценка с ROI, риском и списком блокеров.
    """
    blockers: list[str] = []
    reasons: list[str] = []

    # Весь расчёт ниже — в Stars, поэтому и комиссии приводим к Stars.
    buy_fees = fees_in(session, get_fees(session, buy_market), Currency.STARS)
    sell_fees = fees_in(session, get_fees(session, sell_market), Currency.STARS)

    # Справедливая цена: медиана надёжнее floor, floor — запасной вариант.
    fair_value = snapshot.median_price or snapshot.floor_price or Decimal(0)
    if fair_value <= 0:
        blockers.append("нет рыночной оценки: не на чем считать прибыль")

    if snapshot.confidence is Confidence.NONE:
        blockers.append("качество рыночных данных недостаточно для сделки")

    # Продаём консервативно: не выше floor, если floor ниже медианы.
    expected_sale = fair_value
    if snapshot.floor_price and snapshot.floor_price < fair_value:
        expected_sale = snapshot.floor_price
        reasons.append(
            f"расчёт по floor {snapshot.floor_price}, он ниже медианы {fair_value}"
        )
    if target_markup > 0:
        expected_sale = expected_sale * (Decimal(1) + target_markup)
        reasons.append(f"наценка стратегии +{target_markup:.0%}")

    total_cost = total_cost_of(buy_price, buy_fees)
    net_proceeds = net_proceeds_from(expected_sale, sell_fees)
    net_profit = net_proceeds - total_cost
    net_roi = (net_profit / total_cost) if total_cost > 0 else Decimal(0)

    reasons.append(
        f"комиссия продажи {sell_market.value}: {sell_fees.total_sale_rate:.0%}, "
        f"на руки {net_proceeds:.0f} из {expected_sale:.0f}"
    )
    if buy_market is not sell_market:
        reasons.append(
            f"кросс-рыночная сделка {buy_market.value} -> {sell_market.value}: "
            "атомарность не гарантируется"
        )

    risk_score, risk_reasons = compute_risk(
        snapshot=snapshot,
        buy_price=buy_price,
        fair_value=fair_value,
        market=buy_market,
        is_official_api=is_official_api,
    )
    reasons.extend(risk_reasons)

    if net_profit <= 0:
        blockers.append(
            f"после комиссий сделка убыточна: {net_profit:.0f} Stars"
        )

    return Valuation(
        buy_price=buy_price,
        fair_value=fair_value,
        expected_sale_price=expected_sale,
        net_proceeds=net_proceeds,
        total_cost=total_cost,
        net_profit=net_profit,
        net_roi=net_roi,
        risk_score=risk_score,
        confidence=snapshot.confidence,
        reasons=reasons,
        blockers=blockers,
    )


def break_even_price(
    session: Session,
    *,
    market: Market,
    cost_basis: Decimal,
    currency: Currency = Currency.STARS,
) -> Decimal:
    """Цена, при которой после всех комиссий вернётся себестоимость.

    ``cost_basis`` и результат — в ``currency``. Репрайсер работает в
    валюте площадки (TON), оценка сделки — в Stars, и сетевая комиссия
    должна быть в той же валюте, иначе к цене в TON прибавляются Stars.
    """
    fees = fees_in(session, get_fees(session, market), currency)
    rate = Decimal(1) - fees.total_sale_rate
    if rate <= 0:
        return cost_basis
    return (cost_basis + fees.network_fee) / rate


def price_floor(
    session: Session,
    *,
    market: Market,
    cost_basis: Decimal,
    floor_ratio: Decimal,
    currency: Currency = Currency.STARS,
) -> Decimal:
    """Нижняя граница цены: безубыточность плюс минимальная маржа.

    Ниже этой цены репрайсер не опускается никогда.
    """
    minimum = break_even_price(
        session, market=market, cost_basis=cost_basis, currency=currency
    )
    return round_price(minimum * floor_ratio, currency)


def suggested_list_price(
    session: Session,
    *,
    market: Market,
    cost_basis: Decimal,
    snapshot: MarketSnapshot,
    markup: Decimal,
    floor_ratio: Decimal,
    currency: Currency = Currency.STARS,
) -> Decimal:
    """Подобрать стартовую цену выставления.

    Логика лестницы: начинаем с желаемой наценки над рыночной ценой,
    затем репрайсер шагами снижает цену. Ниже точки безубыточности,
    умноженной на ``floor_ratio``, цена не опускается никогда.

    Если наценка не задана, встаём чуть ниже текущего floor — так лот
    продаётся быстрее.
    """
    minimum = price_floor(
        session,
        market=market,
        cost_basis=cost_basis,
        floor_ratio=floor_ratio,
        currency=currency,
    )

    anchor = snapshot.median_price or snapshot.floor_price
    if markup > 0 and anchor:
        # Старт лестницы: наценка считается от рыночной цены.
        target = anchor * (Decimal(1) + markup)
    elif snapshot.floor_price:
        # Без наценки подрезаем floor на процент, чтобы уйти первыми.
        target = snapshot.floor_price * (Decimal(1) - Decimal("0.01"))
    elif anchor:
        target = anchor
    else:
        target = minimum * (Decimal(1) + markup)

    return max(minimum, round_price(target, currency))
