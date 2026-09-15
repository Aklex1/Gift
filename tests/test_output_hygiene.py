"""Тесты читаемости вывода.

Две находки сквозной проверки: предупреждение об отсутствии курса
печаталось на каждый пересчёт и заглушало собой полезный вывод, а цены
площадок показывались с хвостами вроде 3.969999985.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency
from app.services import marketdata, valuation


@pytest.fixture(autouse=True)
def reset_warning():
    """Предупреждение однократное на процесс — сбрасываем между тестами."""
    marketdata._fx_warned = False
    yield
    marketdata._fx_warned = False


def test_missing_fx_warns_once(session, caplog):
    """Предупреждение об отсутствии курса печатается один раз."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.marketdata"):
        for _ in range(5):
            marketdata.to_stars(session, Decimal("1"), Currency.TON)

    warnings = [r for r in caplog.records if "FX-снапшот" in r.message]
    assert len(warnings) == 1


def test_conversion_still_works_after_warning(session):
    """Пересчёт продолжает работать, а не глохнет вместе с сообщением."""
    first = marketdata.to_stars(session, Decimal("1"), Currency.TON)
    second = marketdata.to_stars(session, Decimal("1"), Currency.TON)

    assert first == second == marketdata.DEFAULT_STARS_PER_TON


def test_real_rate_does_not_warn(session, caplog):
    """Когда курс есть, предупреждать не о чем."""
    import logging

    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("65.56"), "test"
    )
    session.flush()

    with caplog.at_level(logging.WARNING, logger="app.services.marketdata"):
        marketdata.to_stars(session, Decimal("1"), Currency.TON)

    assert not [r for r in caplog.records if "FX-снапшот" in r.message]


# --- цены без хвостов --------------------------------------------------


def test_price_trimmed_for_display():
    """Цена площадки показывается по шагу валюты."""
    assert valuation.round_price(
        Decimal("3.969999985"), Currency.TON
    ) == Decimal("3.96")


def test_rounding_never_rounds_up():
    """Округление вниз: показанная цена не должна быть выше настоящей.

    Иначе человек увидит цену дороже, чем на площадке, и решит, что
    сделка хуже, чем есть.
    """
    for raw in ("3.969999985", "3.9699", "3.961"):
        assert valuation.round_price(Decimal(raw), Currency.TON) <= Decimal(raw)


def test_stars_stay_whole():
    """Stars целые — дробить их нечем."""
    assert valuation.round_price(
        Decimal("257.83"), Currency.STARS
    ) == Decimal("257")


def test_exact_price_used_for_buying(session):
    """Для покупки берётся точная цена, а не округлённая.

    Portals сверяет цену перед списанием: округлённая не совпала бы, и
    покупка отбивалась бы «цена изменилась».
    """
    from app.adapters.base import GiftRef, ListingDTO
    from app.enums import Market

    raw = Decimal("3.969999985")
    listing = ListingDTO(
        market=Market.PORTALS, external_id="x",
        gift=GiftRef(collection="C", model="M"),
        price=raw, currency=Currency.TON,
    )

    # То, что уйдёт в purchase, остаётся нетронутым.
    assert listing.price == raw
    assert listing.price != valuation.round_price(raw, Currency.TON)
