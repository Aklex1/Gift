"""Тесты перечитывания лота перед покупкой.

Каждая покупка на Portals отбивалась сообщением «лот больше не
выставлен». Причина: лот искали через `query=<id>`, а это текстовый
поиск — он игнорирует идентификатор и возвращает посторонние лоты.
Совпадение не находилось никогда.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Capability, CapabilityStatus


def _adapter():
    """Адаптер Portals с разрешённой покупкой."""
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api")
    adapter.capabilities[Capability.BUY] = CapabilityStatus.EXPERIMENTAL
    return adapter


def _item(ident, price="3.95"):
    """Ответ площадки об одном лоте."""
    return {
        "id": ident,
        "price": price,
        "collection_name": "Lol Pop",
        "external_collection_number": 1,
    }


@pytest.mark.asyncio
async def test_direct_endpoint_used():
    """Запрашивается конкретный лот, а не поиск."""
    adapter = _adapter()
    seen = {}

    async def request(method, path, **kwargs):
        seen.update(method=method, path=path, params=kwargs.get("params"))
        return _item("abc-123")

    adapter.request = request
    await adapter.fetch_listing("abc-123")

    assert seen["path"] == "/nfts/abc-123"
    assert not seen["params"]


@pytest.mark.asyncio
async def test_listing_returned_with_price():
    """Лот возвращается вместе с ценой — по ней сверяется покупка."""
    adapter = _adapter()

    async def request(method, path, **kwargs):
        return _item("abc-123", "3.95")

    adapter.request = request
    fresh = await adapter.fetch_listing("abc-123")

    assert fresh is not None
    assert fresh.external_id == "abc-123"
    assert fresh.price == Decimal("3.95")


@pytest.mark.asyncio
async def test_foreign_listing_rejected():
    """Чужой лот в ответе не принимается за наш.

    Ровно это и происходило при поиске по query: возвращался другой
    лот, и покупка ушла бы не туда, если бы совпадение не проверялось.
    """
    adapter = _adapter()

    async def request(method, path, **kwargs):
        return _item("совсем-другой")

    adapter.request = request

    assert await adapter.fetch_listing("abc-123") is None


@pytest.mark.asyncio
async def test_missing_listing_is_none():
    """Пропавший лот — None, а не исключение."""
    adapter = _adapter()

    async def request(method, path, **kwargs):
        raise RuntimeError("404 Not Found")

    adapter.request = request

    assert await adapter.fetch_listing("abc-123") is None


@pytest.mark.asyncio
async def test_empty_id_short_circuits():
    """Пустой идентификатор не идёт в сеть."""
    adapter = _adapter()
    called = False

    async def request(method, path, **kwargs):
        nonlocal called
        called = True
        return {}

    adapter.request = request

    assert await adapter.fetch_listing("") is None
    assert called is False


@pytest.mark.asyncio
async def test_buy_proceeds_when_listing_matches(monkeypatch):
    """Покупка доходит до исполнения, когда лот на месте и цена та же."""
    adapter = _adapter()

    async def request(method, path, **kwargs):
        return _item("abc-123", "3.95")

    async def contract(op, *, external_id, price):
        from app.adapters.base import ExecutionResult

        return ExecutionResult(ok=True, detail="куплено")

    adapter.request = request
    adapter.execute_contract_op = contract

    result = await adapter.buy(
        external_id="abc-123", expected_price=Decimal("3.95"), idempotency_key="k"
    )

    assert result.ok is True


@pytest.mark.asyncio
async def test_buy_refuses_on_price_change():
    """Изменившаяся цена — отказ до отправки платежа."""
    adapter = _adapter()

    async def request(method, path, **kwargs):
        return _item("abc-123", "9.99")

    adapter.request = request

    result = await adapter.buy(
        external_id="abc-123", expected_price=Decimal("3.95"), idempotency_key="k"
    )

    assert result.ok is False
    assert "цена изменилась" in result.detail
