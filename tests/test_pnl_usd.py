"""Тесты показа прибыли в деньгах.

«+380 ★» не говорит, много это или мало: звезда — не та единица, в
которой человек меряет результат. Доллар говорит сразу, поэтому он
показывается рядом.

Но пересчёт по курсу — ровно то место, где у нас уже был самый дорогой
баг: устаревший курс рисовал ROI в тысячи процентов. Поэтому здесь
проверяется не столько арифметика, сколько отказ: без свежего курса
показывается «—», а не число.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Currency
from app.models import FxSnapshot, utcnow
from app.services import marketdata


def _rate(session, base, quote, value, *, age_days=0):
    session.add(
        FxSnapshot(
            base=base, quote=quote, rate=Decimal(str(value)), source="test",
            taken_at=utcnow() - dt.timedelta(days=age_days),
        )
    )
    session.flush()


def test_stars_convert_to_dollars(session):
    """Прибыль в звёздах переводится в доллары по свежему курсу."""
    _rate(session, Currency.STARS, Currency.USD, "0.013")

    assert marketdata.to_usd(
        session, Decimal(1000), Currency.STARS
    ) == Decimal("13.000")


def test_gram_converts_through_the_pivot(session):
    """Курс GRAM → доллар берётся напрямую, когда он записан."""
    _rate(session, Currency.TON, Currency.USD, "3.10")

    assert marketdata.to_usd(session, Decimal(2), Currency.TON) == Decimal("6.20")


def test_a_stale_rate_is_refused(session):
    """Курс месячной давности не используется — лучше промолчать.

    Снапшот не перезаписывается, когда источник недоступен, поэтому
    старое значение продолжает лежать в базе и выглядеть рабочим.
    Именно так у нас однажды появился курс 400 вместо 100.
    """
    _rate(session, Currency.STARS, Currency.USD, "0.013", age_days=30)

    assert marketdata.to_usd(session, Decimal(1000), Currency.STARS) is None


def test_no_rate_is_not_zero(session):
    """Без курса прибыль неизвестна, а не равна нулю."""
    assert marketdata.to_usd(session, Decimal(1000), Currency.STARS) is None


def test_dollars_stay_dollars(session):
    """Сумма уже в долларах не пересчитывается."""
    assert marketdata.to_usd(
        session, Decimal("12.5"), Currency.USD
    ) == Decimal("12.5")
