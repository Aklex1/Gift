"""Суточные лимиты расходов.

Бюджет стратегии ограничивает общий объём вложений, но не скорость.
Ошибка в фильтрах или резкое движение рынка могут за час выбрать весь
бюджет. Суточный лимит — предохранитель именно от этого.

Лимиты два: общий на всю систему и отдельный на каждую площадку.
Срабатывает более строгий из них.

Расход считается по фактическим транзакциям покупок за текущие сутки
UTC, а не по отдельному счётчику: счётчик рассинхронизировался бы при
сбое или откате, а журнал сделок — источник правды.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import Currency, Market
from app.models import Transaction
from app.services import runtime, store

log = logging.getLogger(__name__)

KEY_DAILY_TOTAL = "DAILY_LIMIT_STARS"


def _daily_key(market: Market) -> str:
    """Имя настройки суточного лимита площадки."""
    unit = "STARS" if runtime.cap_currency(market) is Currency.STARS else "TON"
    return f"{market.value.upper()}_DAILY_LIMIT_{unit}"


def day_start(now: dt.datetime | None = None) -> dt.datetime:
    """Начало текущих суток UTC."""
    now = now or dt.datetime.utcnow()
    return dt.datetime(now.year, now.month, now.day)


# ----------------------------------------------------------------------
# Лимиты
# ----------------------------------------------------------------------
def daily_total_limit() -> Decimal | None:
    """Общий суточный лимит в Stars. None — не задан."""
    raw = store.get(KEY_DAILY_TOTAL)
    value = runtime._as_decimal(raw, Decimal(settings.daily_limit_stars or 0))
    return value if value > 0 else None


def daily_market_limit(market: Market) -> Decimal | None:
    """Суточный лимит площадки в её валюте. None — не задан."""
    value = runtime._as_decimal(store.get(_daily_key(market)), Decimal(0))
    return value if value > 0 else None


def set_daily_total_limit(value: Decimal, *, actor: str = "web") -> None:
    """Задать общий суточный лимит."""
    store.set(KEY_DAILY_TOTAL, format(value.normalize(), "f") if value > 0 else "")
    runtime._audit(actor, "daily_limit_total", str(value))


def set_daily_market_limit(
    market: Market, value: Decimal, *, actor: str = "web"
) -> None:
    """Задать суточный лимит площадки."""
    store.set(_daily_key(market), format(value.normalize(), "f") if value > 0 else "")
    runtime._audit(actor, f"daily_limit:{market.value}", str(value))


# ----------------------------------------------------------------------
# Расход
# ----------------------------------------------------------------------
def spent_today(session: Session, market: Market | None = None) -> Decimal:
    """Сколько потрачено на покупки за текущие сутки.

    Без указания площадки считается общая сумма в Stars; с указанием —
    сумма в валюте этой площадки.
    """
    stmt = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.kind == "buy",
        Transaction.happened_at >= day_start(),
    )
    if market is not None:
        stmt = stmt.where(Transaction.market == market)
    else:
        # Общий лимит ведётся в Stars — складываем только их.
        stmt = stmt.where(Transaction.currency == Currency.STARS)

    return Decimal(session.execute(stmt).scalar_one() or 0)


def remaining_today(session: Session, market: Market | None = None) -> Decimal | None:
    """Остаток суточного лимита. None — лимит не задан."""
    limit = daily_market_limit(market) if market is not None else daily_total_limit()
    if limit is None:
        return None
    return max(Decimal(0), limit - spent_today(session, market))


def check(
    session: Session, *, market: Market, amount: Decimal, amount_stars: Decimal
) -> str:
    """Проверить, укладывается ли покупка в суточные лимиты.

    Args:
        amount: сумма в валюте площадки.
        amount_stars: та же сумма, приведённая к Stars, — для общего лимита.

    Returns:
        Пустая строка, если можно покупать, иначе причина отказа.
    """
    market_limit = daily_market_limit(market)
    if market_limit is not None:
        spent = spent_today(session, market)
        if spent + amount > market_limit:
            currency = runtime.cap_currency(market).value
            return (
                f"суточный лимит {market.value} исчерпан: потрачено "
                f"{spent:.2f} из {market_limit:.2f} {currency}, "
                f"сделка на {amount:.2f} не помещается"
            )

    total_limit = daily_total_limit()
    if total_limit is not None:
        spent = spent_today(session)
        if spent + amount_stars > total_limit:
            return (
                f"общий суточный лимит исчерпан: потрачено {spent:.0f} "
                f"из {total_limit:.0f} Stars, сделка на {amount_stars:.0f} "
                f"не помещается"
            )

    return ""


def snapshot(session: Session) -> dict:
    """Срез суточных лимитов для интерфейса."""
    total_limit = daily_total_limit()
    out = {
        "total": {
            "limit": total_limit,
            "spent": spent_today(session),
            "remaining": remaining_today(session),
            "currency": Currency.STARS.value,
        },
        "markets": {},
    }
    for market in runtime.TRADABLE:
        out["markets"][market.value] = {
            "market": market,
            "limit": daily_market_limit(market),
            "spent": spent_today(session, market),
            "remaining": remaining_today(session, market),
            "currency": runtime.cap_currency(market).value,
        }
    return out
