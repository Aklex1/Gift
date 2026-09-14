"""Оценка сделки: ROI с учётом комиссий, риск и объяснимое решение.

Раздел 6 ТЗ: fee-aware ROI, liquidity, risk 0-100, explainable decision.

Главное правило расчёта: прибыль считается от суммы, которая реально
дойдёт до нас после всех комиссий площадки и роялти — иначе «прибыльная»
сделка оказывается убыточной.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from app.enums import Confidence, Currency, Market
from app.models import FeeSchedule
from app.services.marketdata import MarketSnapshot

log = logging.getLogger(__name__)

#: Комиссии по умолчанию, если в БД нет актуальной версии.
#: Telegram удерживает комиссию с продавца при перепродаже подарка.
DEFAULT_FEES: dict[Market, dict[str, Decimal]] = {
    Market.TELEGRAM: {
        "sale_fee": Decimal("0.20"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0"),
    },
    Market.PORTALS: {
        "sale_fee": Decimal("0.05"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.1"),
    },
    Market.MRKT: {
        "sale_fee": Decimal("0.05"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.1"),
    },
    Market.TONNEL: {
        "sale_fee": Decimal("0.06"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0"),
        "network_fee": Decimal("0.1"),
    },
    Market.GETGEMS: {
        "sale_fee": Decimal("0.05"),
        "buy_fee": Decimal("0"),
        "royalty": Decimal("0.05"),
        "network_fee": Decimal("0.1"),
    },
}


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
                currency=Currency.STARS,
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

    buy_fees = get_fees(session, buy_market)
    sell_fees = get_fees(session, sell_market)

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


def suggested_list_price(
    session: Session,
    *,
    market: Market,
    cost_basis: Decimal,
    snapshot: MarketSnapshot,
    markup: Decimal,
    floor_ratio: Decimal,
) -> Decimal:
    """Подобрать цену выставления.

    Цена не опускается ниже точки безубыточности с учётом комиссий,
    умноженной на ``floor_ratio`` — минимальную желаемую маржу.
    """
    fees = get_fees(session, market)

    # Цена, при которой после комиссий вернём себестоимость.
    rate = Decimal(1) - fees.total_sale_rate
    break_even = (
        (cost_basis + fees.network_fee) / rate if rate > 0 else cost_basis
    )
    minimum = break_even * floor_ratio

    anchor = snapshot.floor_price or snapshot.median_price
    if anchor:
        # Встаём чуть ниже текущего floor, чтобы продать быстрее.
        target = anchor * (Decimal(1) - Decimal("0.01"))
        if markup > 0 and snapshot.median_price:
            target = max(target, snapshot.median_price * (Decimal(1) + markup))
    else:
        target = minimum * (Decimal(1) + markup)

    return max(minimum, target).quantize(Decimal("1"))
