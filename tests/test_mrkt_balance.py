"""Тесты баланса MRKT.

В панели вместо баланса стоял текст ошибки HTTP: адаптер спрашивал
``/users/me``, а такого пути у площадки нет — 404. Настоящий путь
``/balance``, и отвечает он не одним числом, а списком всех внутренних
счетов сразу:

    {"soft": 0, "hard": 0, "stars": 0, "spices": 0,
     "stackingPoints": 0, "nanoUSDs": 0, ...}

Отсюда главное, что здесь проверяется: деньгами считаются только GRAM и
звёзды. ``soft``, ``spices`` и очки — игровая механика мини-приложения,
подарки за них не покупаются, и показать их как баланс значит нарисовать
бюджет, которого нет.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Capability, CapabilityStatus, Currency, Market


def _adapter(payload, monkeypatch):
    from app.adapters.mrkt import MrktAdapter

    adapter = MrktAdapter(base_url="https://api.tgmrkt.io/api/v1", auth="x")
    adapter.capabilities[Capability.BALANCE] = CapabilityStatus.EXPERIMENTAL
    asked: list[tuple[str, str]] = []

    async def request(method, path, **_kwargs):
        asked.append((method, path))
        return payload

    adapter.request = request
    adapter.asked = asked
    return adapter


@pytest.mark.asyncio
async def test_balance_comes_from_its_own_endpoint(monkeypatch):
    """Спрашиваем /balance: /users/me у площадки нет."""
    adapter = _adapter({"hard": 0, "stars": 0}, monkeypatch)

    await adapter.balance()

    assert adapter.asked == [("GET", "/balance")]


@pytest.mark.asyncio
async def test_gram_comes_in_nano(monkeypatch):
    """Суммы названы в нанотонах — как и цены лотов."""
    adapter = _adapter({"hard": 8096529000, "stars": 0}, monkeypatch)

    rows = await adapter.balance()

    assert rows[0].currency is Currency.TON
    assert rows[0].amount == Decimal("8.096529")


@pytest.mark.asyncio
async def test_stars_are_shown_separately(monkeypatch):
    """Звёзды — отдельная строка, а не слагаемое к GRAM."""
    adapter = _adapter({"hard": 1000000000, "stars": 250}, monkeypatch)

    rows = await adapter.balance()

    assert [(r.currency, r.amount) for r in rows] == [
        (Currency.TON, Decimal(1)),
        (Currency.STARS, Decimal(250)),
    ]


@pytest.mark.asyncio
async def test_game_points_are_not_money(monkeypatch):
    """Очки мини-приложения в баланс не идут.

    За spices и stackingPoints подарок не купить, а в строке баланса
    они выглядели бы доступными деньгами.
    """
    adapter = _adapter(
        {"hard": 0, "stars": 0, "soft": 5000, "spices": 120,
         "stackingPoints": 900, "nanoUSDs": 7000000},
        monkeypatch,
    )

    rows = await adapter.balance()

    assert len(rows) == 1
    assert rows[0].amount == Decimal(0)


@pytest.mark.asyncio
async def test_zero_balance_is_still_reported(monkeypatch):
    """Ноль — это ответ. Пустая строка выглядела бы поломкой связи."""
    adapter = _adapter({"hard": 0, "stars": 0}, monkeypatch)

    rows = await adapter.balance()

    assert len(rows) == 1
    assert rows[0].amount == Decimal(0)


@pytest.mark.asyncio
async def test_unexpected_answer_does_not_raise(monkeypatch):
    """Ответ не того вида — пустой список, а не исключение в панели."""
    adapter = _adapter(["не словарь"], monkeypatch)

    assert await adapter.balance() == []
