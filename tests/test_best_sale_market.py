"""Тесты выбора площадки для продажи.

Комиссии различаются в разы: Telegram берёт 20% с продажи, Portals
около 2.5%. Тот же лот, перепроданный на другой площадке, приносит
заметно больше — но только если он там действительно продаётся.
Поэтому цена продажи берётся из данных самой этой площадки, а не
переносится с той, где покупаем.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Currency, Market
from app.models import Gift, Listing, utcnow
from app.services import valuation


@pytest.fixture(autouse=True)
def rates(session, monkeypatch):
    """Фиксированный курс и комиссии по умолчанию."""
    from app.services import arbitrage, marketdata, store

    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()
    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )
    monkeypatch.setattr(arbitrage, "transfer_cost_ton", lambda: Decimal("0.1"))
    valuation.seed_fee_schedules(session)
    session.flush()
    yield
    store.invalidate()


def _listing(session, market, collection, model, price_stars, number=1):
    """Активный лот на площадке."""
    gift = Gift(
        canonical_key=f"{collection}#{number}",
        collection=collection,
        model=model,
        number=number,
    )
    session.add(gift)
    session.flush()
    session.add(
        Listing(
            market=market,
            external_id=f"{market}-{number}",
            gift_id=gift.id,
            price=Decimal(str(price_stars)),
            currency=Currency.STARS,
            price_stars=Decimal(str(price_stars)),
            is_active=True,
            seen_at=utcnow(),
        )
    )
    session.flush()


def _best(session, **kwargs):
    """Вызов с обычными параметрами."""
    params = {
        "buy_market": Market.TELEGRAM,
        "buy_price": Decimal("450"),
        "collection": "Chill Flame",
        "model": "Oil Lamp",
    }
    params.update(kwargs)
    return valuation.best_sale_market(session, **params)


# --- основное поведение -----------------------------------------------


def test_lower_fee_market_wins(session):
    """При равной цене выигрывает площадка с меньшей комиссией."""
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 609)

    best = _best(session)

    assert best is not None
    assert best["market"] == "portals"
    # 609 × 0.975 − 3.25 сеть − 6.5 перенос − 450 = 134 прибыли.
    assert best["net_roi"] > Decimal("0.25")


def test_no_data_means_no_hint(session):
    """Без лотов площадки подсказки нет: цену взять неоткуда.

    Подставить сюда цену другого рынка значило бы нарисовать прибыль.
    """
    assert _best(session) is None


def test_other_model_does_not_count(session):
    """Данные другой модели не годятся: цена внутри коллекции разная."""
    _listing(session, Market.PORTALS, "Chill Flame", "Другая", 609)

    assert _best(session) is None


def test_other_collection_does_not_count(session):
    """И другая коллекция тоже."""
    _listing(session, Market.PORTALS, "Другая", "Oil Lamp", 609)

    assert _best(session) is None


def test_buy_market_itself_excluded(session):
    """Площадка покупки не предлагается как «другая»."""
    _listing(session, Market.TELEGRAM, "Chill Flame", "Oil Lamp", 609)

    assert _best(session) is None


def test_unprofitable_elsewhere_is_not_offered(session):
    """Если и там убыток, подсказки нет."""
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 300)

    assert _best(session) is None


def test_cheapest_listing_is_the_anchor(session):
    """Ориентир — минимум площадки: продавать придётся не дороже."""
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 900, number=1)
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 609, number=2)

    assert _best(session)["sale_price"] == Decimal("609")


def test_best_of_several_markets(session):
    """Из нескольких площадок выбирается самая выгодная."""
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 609, number=1)
    _listing(session, Market.MRKT, "Chill Flame", "Oil Lamp", 700, number=2)

    best = _best(session)

    # У MRKT комиссия выше (5% против 2.5%), но цена заметно больше.
    assert best["market"] == "mrkt"


def test_transfer_cost_subtracted(session):
    """Перенос подарка вычитается: дорогой перенос съедает выгоду."""
    from app.services import arbitrage

    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 609)
    with_cheap = _best(session)["net_roi"]

    arbitrage.transfer_cost_ton = lambda: Decimal("2")
    with_pricey = _best(session)

    assert with_pricey is None or with_pricey["net_roi"] < with_cheap


def test_note_explains_the_difference(session):
    """Подсказка объясняет, почему там выгоднее и что перенос ручной."""
    _listing(session, Market.PORTALS, "Chill Flame", "Oil Lamp", 609)

    note = _best(session)["note"]

    assert "portals" in note
    assert "комиссия" in note
    assert "вручную" in note
