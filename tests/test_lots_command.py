"""Тесты команды `gift-cli lots`.

Она отвечает на вопрос, который не решают ни probe, ни scan: probe
говорит лишь «площадка жива», а scan уже отфильтрован стратегиями.
Здесь различаются три случая, которые в отчёте выглядели одинаково:
площадка не работает, работает но пуста, работает и отдаёт лоты.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import AuthRequired, Capability, GiftRef, ListingDTO
from app.enums import Currency, Market


class _Adapter:
    """Площадка с заданным поведением поиска."""

    def __init__(self, outcome=None, *, supports=True):
        self.outcome = [] if outcome is None else outcome
        self._supports = supports
        self.asked = []

    def supports(self, _capability: Capability) -> bool:
        return self._supports

    async def search(self, *, collection=None, limit=20):
        """Отдать запланированный исход."""
        self.asked.append(collection)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _lot(collection, number, price, currency, model=None):
    """Один лот площадки."""
    return ListingDTO(
        market=Market.PORTALS,
        external_id=str(number),
        gift=GiftRef(collection=collection, number=number, model=model),
        price=Decimal(str(price)),
        currency=currency,
    )


@pytest.fixture(autouse=True)
def wiring(session, monkeypatch):
    """Подменить БД, курс и сверку коллекций."""
    from contextlib import contextmanager

    import app.db as db_module
    from app import cli
    from app.services import marketdata

    @contextmanager
    def scope():
        yield session
        session.flush()

    # Команда импортирует то и другое внутри себя, поэтому подменяем
    # в самом модуле app.db, а не в app.cli.
    monkeypatch.setattr(db_module, "init_db", lambda: None)
    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )

    async def no_check():
        """Сверку коллекций проверяем отдельно."""

    monkeypatch.setattr(cli, "_check_strategy_collections", no_check)
    yield


def _install(monkeypatch, mapping):
    """Подменить реестр адаптеров."""
    monkeypatch.setattr(
        "app.adapters.registry.get_adapter", lambda m: mapping[m]
    )


@pytest.mark.asyncio
async def test_lots_are_listed_with_prices(monkeypatch, capsys):
    """Лоты показываются с ценой в валюте площадки и в Stars."""
    from app import cli

    _install(monkeypatch, {
        Market.PORTALS: _Adapter([_lot("Plush Pepe", 777, "12.5", Currency.TON, "Neon")]),
    })

    assert await cli._lots("portals", None) == 0
    out = capsys.readouterr().out

    assert "Лотов получено" in out
    assert "Plush Pepe #777" in out
    assert "12.5 GRAM" in out
    # 12.5 × 65 = 812 Stars, с пробелом как разделителем разрядов.
    assert "812" in out


@pytest.mark.asyncio
async def test_empty_market_is_not_called_broken(monkeypatch, capsys):
    """Пустой ответ — не поломка, и так и написано."""
    from app import cli

    _install(monkeypatch, {Market.PORTALS: _Adapter([])})

    assert await cli._lots("portals", None) == 0
    out = capsys.readouterr().out

    assert "предложений нет" in out
    assert "не поломка" in out


@pytest.mark.asyncio
async def test_missing_token_explained(monkeypatch, capsys):
    """Без токена причина названа прямо."""
    from app import cli

    _install(monkeypatch, {Market.PORTALS: _Adapter(supports=False)})

    assert await cli._lots("portals", None) == 1
    out = capsys.readouterr().out

    assert "нет токена площадки" in out


@pytest.mark.asyncio
async def test_telegram_without_session_explained(monkeypatch, capsys):
    """Для Telegram причина — вход, а не токен."""
    from app import cli

    _install(monkeypatch, {Market.TELEGRAM: _Adapter(supports=False)})

    await cli._lots("telegram", None)

    assert "gift-cli login" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_auth_failure_shown(monkeypatch, capsys):
    """Отказ доступа показывается, а команда не падает."""
    from app import cli

    _install(monkeypatch, {
        Market.PORTALS: _Adapter(AuthRequired("portals: нет доступа (401)")),
    })

    assert await cli._lots("portals", None) == 1
    out = capsys.readouterr().out

    assert "Поиск не удался" in out
    assert "401" in out


@pytest.mark.asyncio
async def test_collection_filter_passed_through(monkeypatch, capsys):
    """Указанная коллекция доходит до площадки."""
    from app import cli

    adapter = _Adapter([])
    _install(monkeypatch, {Market.PORTALS: adapter})

    await cli._lots("portals", "Lol Pop")

    assert adapter.asked == ["Lol Pop"]
    assert "'Lol Pop'" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unknown_market_rejected(capsys):
    """Опечатка в имени площадки — понятный отказ."""
    from app import cli

    assert await cli._lots("порталс", None) == 1
    assert "Неизвестная площадка" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cheapest_first(monkeypatch, capsys):
    """Лоты показываются от дешёвых к дорогим: там и ищут недооценку."""
    from app import cli

    _install(monkeypatch, {
        Market.PORTALS: _Adapter([
            _lot("B", 2, "50", Currency.TON),
            _lot("A", 1, "10", Currency.TON),
        ]),
    })

    await cli._lots("portals", None)
    out = capsys.readouterr().out

    assert out.index("A #1") < out.index("B #2")
