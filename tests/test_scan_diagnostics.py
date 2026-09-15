"""Тесты объяснимости пустого прохода сканера.

«Просмотрено лотов 0» раньше выглядело одинаково при совершенно
разных бедах: нет токена, опечатка в названии коллекции, лимит
запросов, площадка действительно пуста. В панели оставалась догадка
«проверьте токены и сеть», по которой ничего не найти.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import (
    AuthRequired,
    Capability,
    CapabilityStatus,
    GiftRef,
    ListingDTO,
    RateLimited,
    SearchSkipped,
)
from app.enums import Currency, Market
from app.services import scanner


class _Adapter:
    """Площадка с заданным поведением поиска."""

    def __init__(self, *, label="portals", outcome=None, supports=True):
        self.label = label
        self.outcome = outcome if outcome is not None else []
        self._supports = supports

    def supports(self, _capability: Capability) -> bool:
        return self._supports

    async def search(self, *, collection=None, limit=100):
        """Отдать запланированный исход."""
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(collection)
        return self.outcome


def _listing(collection="Pepe"):
    """Один лот."""
    return ListingDTO(
        market=Market.PORTALS,
        external_id="1",
        gift=GiftRef(collection=collection, model="neon"),
        price=Decimal("10"),
        currency=Currency.TON,
    )


@pytest.fixture()
def adapters(monkeypatch):
    """Подменить набор адаптеров поиска."""

    def install(items):
        monkeypatch.setattr(scanner, "_search_adapters", lambda _m: items)

    return install


# --- причина известна --------------------------------------------------


@pytest.mark.asyncio
async def test_no_adapters_at_all(adapters):
    """Нет ни одного аккаунта — так и сказано."""
    adapters([])
    rows, notes = await scanner.collect_listings(
        Market.TELEGRAM, collections=[], limit=10
    )

    assert rows == []
    assert any("ни одного аккаунта" in n for n in notes)


@pytest.mark.asyncio
async def test_telegram_without_session_explained(adapters):
    """Для Telegram причина — неавторизованная сессия, а не «токен»."""
    adapters([_Adapter(label="основной", supports=False)])
    _, notes = await scanner.collect_listings(
        Market.TELEGRAM, collections=[], limit=10
    )

    assert any("gift-cli login" in n for n in notes)


@pytest.mark.asyncio
async def test_market_without_token_explained(adapters):
    """Для площадки с токеном причина — отсутствующий токен."""
    adapters([_Adapter(supports=False)])
    _, notes = await scanner.collect_listings(
        Market.PORTALS, collections=[], limit=10
    )

    assert any("токен" in n for n in notes)


@pytest.mark.asyncio
async def test_auth_error_reported(adapters):
    """401 от площадки виден в отчёте, а не только в журнале."""
    adapters([_Adapter(outcome=AuthRequired("portals: нет доступа (401)"))])
    rows, notes = await scanner.collect_listings(
        Market.PORTALS, collections=["Pepe"], limit=10
    )

    assert rows == []
    assert any("токен не принят" in n for n in notes)


@pytest.mark.asyncio
async def test_rate_limit_reported(adapters):
    """Упёрлись в лимит Telegram — это отдельная причина."""
    adapters([_Adapter(label="основной", outcome=RateLimited("FloodWait 300"))])
    _, notes = await scanner.collect_listings(
        Market.TELEGRAM, collections=["Pepe"], limit=10
    )

    assert any("лимит запросов" in n for n in notes)


@pytest.mark.asyncio
async def test_empty_market_reported(adapters):
    """Площадка ответила пусто — это тоже названо прямо."""
    adapters([_Adapter(outcome=[])])
    _, notes = await scanner.collect_listings(
        Market.PORTALS, collections=["Pepe", "Lol Pop"], limit=10
    )

    assert any("ответила пусто" in n for n in notes)
    assert any("Pepe" in n for n in notes)


# --- одна коллекция не должна ронять остальные ------------------------


@pytest.mark.asyncio
async def test_unknown_collection_does_not_stop_the_rest(adapters):
    """Опечатка в одной коллекции не отменяет осмотр остальных.

    Это и есть разница между SearchSkipped и отказом площадки.
    """

    def outcome(collection):
        if collection == "Опечатка":
            raise SearchSkipped("коллекция 'Опечатка' не найдена в каталоге")
        return [_listing(collection)]

    adapters([_Adapter(outcome=outcome)])
    rows, notes = await scanner.collect_listings(
        Market.TELEGRAM, collections=["Опечатка", "Pepe"], limit=10
    )

    assert len(rows) == 1
    assert rows[0].gift.collection == "Pepe"
    assert any("не найдена" in n for n in notes)


@pytest.mark.asyncio
async def test_auth_error_stops_that_adapter(adapters):
    """А вот отказ доступа выводит аккаунт из игры: смысла долбиться нет."""
    calls = []

    def outcome(collection):
        calls.append(collection)
        raise AuthRequired("нет доступа (401)")

    adapters([_Adapter(outcome=outcome)])
    await scanner.collect_listings(
        Market.PORTALS, collections=["A", "B", "C"], limit=10
    )

    assert calls == ["A"]


@pytest.mark.asyncio
async def test_success_reports_no_noise(adapters):
    """Когда лоты есть, лишних причин в отчёте не появляется."""
    adapters([_Adapter(outcome=[_listing()])])
    rows, notes = await scanner.collect_listings(
        Market.PORTALS, collections=["Pepe"], limit=10
    )

    assert len(rows) == 1
    assert notes == []


@pytest.mark.asyncio
async def test_partial_success_keeps_listings(adapters):
    """Пустая коллекция рядом с непустой не превращается в жалобу."""

    def outcome(collection):
        return [_listing(collection)] if collection == "Pepe" else []

    adapters([_Adapter(outcome=outcome)])
    rows, notes = await scanner.collect_listings(
        Market.PORTALS, collections=["Pepe", "Пусто"], limit=10
    )

    assert len(rows) == 1
    # Лоты есть — значит площадка работает, и жаловаться не на что.
    assert notes == []
