"""Тесты: оцениваемый лот не подтверждает сам себя.

В панели это выглядело так:

    источник  медиана  floor  лотов
    own       —        398    1

Единственный лот в «собственной выборке» — тот самый, который мы и
оцениваем. Его цена становилась floor'ом выборки, и в обосновании это
читалось как независимое подтверждение. Оно же попадало в потолок
дешёвой отсечки.

Своё наблюдение — это чужой лот. Свой не считается.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import GiftRef, ListingDTO
from app.enums import Currency, Market
from app.models import Gift, Listing, utcnow
from app.services import marketdata, scanner


def _listing(session, external_id, price, number, market=Market.PORTALS):
    gift = Gift(
        canonical_key=f"Lol Pop#{number}", collection="Lol Pop",
        model="Orangery", number=number,
    )
    session.add(gift)
    session.flush()
    session.add(
        Listing(
            market=market, external_id=external_id, gift_id=gift.id,
            price=Decimal(str(price)), currency=Currency.STARS,
            price_stars=Decimal(str(price)), is_active=True, seen_at=utcnow(),
        )
    )
    session.flush()


def _snapshot(session, exclude=None, **kwargs):
    return marketdata.snapshot_for(
        session, collection="Lol Pop", model="Orangery",
        exclude=exclude, **kwargs
    )


def _dto(external_id="p1", price="399"):
    return ListingDTO(
        market=Market.PORTALS,
        external_id=external_id,
        gift=GiftRef(collection="Lol Pop", model="Orangery", slug="lolpop-1"),
        price=Decimal(price),
        currency=Currency.STARS,
    )


def test_alone_on_the_market_means_no_own_data(session):
    """Единственный лот — это отсутствие выборки, а не выборка из одного."""
    _listing(session, "p1", 399, 1)

    snapshot = _snapshot(session, exclude=(Market.PORTALS, "p1"))

    assert snapshot.floor_price is None
    assert snapshot.active_listings == 0


def test_other_lots_still_count(session):
    """Чужие лоты остаются наблюдением — исключается только свой."""
    _listing(session, "p1", 399, 1)
    _listing(session, "p2", 500, 2)
    _listing(session, "p3", 520, 3)

    snapshot = _snapshot(session, exclude=(Market.PORTALS, "p1"))

    assert snapshot.floor_price == Decimal(500)
    assert snapshot.active_listings == 2


def test_same_id_on_another_market_is_not_us(session):
    """Совпадение внешнего id на другой площадке — другой лот."""
    _listing(session, "p1", 399, 1, market=Market.TELEGRAM)

    snapshot = _snapshot(session, exclude=(Market.PORTALS, "p1"))

    assert snapshot.active_listings == 1


def test_without_exclusion_behaviour_is_unchanged(session):
    """Без указания лота срез считается как раньше."""
    _listing(session, "p1", 399, 1)

    assert _snapshot(session).active_listings == 1


def test_cheap_ceiling_ignores_the_lot_itself(session):
    """Потолок отсечки не строится на цене самого лота.

    Иначе дорогой лот сам себе поднимал бы потолок и проходил
    проверку, которую должен был не пройти.
    """
    _listing(session, "p1", 5000, 1)

    assert scanner.cheap_ceiling(session, _dto("p1", "5000")) is None


def test_cheap_ceiling_uses_the_neighbours(session):
    """А соседние лоты потолок задают."""
    _listing(session, "p1", 5000, 1)
    _listing(session, "p2", 400, 2)

    ceiling = scanner.cheap_ceiling(session, _dto("p1", "5000"), margin=Decimal(1))

    assert ceiling == Decimal(400)


def test_lone_expensive_lot_is_not_cut_silently(session):
    """Одинокий дорогой лот не отсеивается — о нём просто нечего сказать.

    Раньше он подтверждал сам себя; теперь про него не известно
    ничего, и отсечка честно воздерживается от суждения.
    """
    _listing(session, "p1", 5000, 1)

    assert not scanner.hopeless(
        session, _dto("p1", "5000"), Decimal("5000"), Decimal("0.1")
    )
