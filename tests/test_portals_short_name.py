"""Тесты короткого имени коллекции для Portals.

Площадка принимает `plushpepe`, а не `Plush Pepe`. На отображаемое имя
эндпоинт отвечает пустыми списками — без ошибки, просто без данных.
Из-за этого floor по моделям не приходил вовсе, оценка откатывалась на
пустую историю продаж, и каждый лот Portals отбраковывался как «мало
рыночных данных».
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.portals import PortalsAdapter, short_collection_name


def test_display_name_becomes_short_name():
    """Пробелы и дефисы убираются, регистр опускается."""
    assert short_collection_name("Plush Pepe") == "plushpepe"
    assert short_collection_name("B-Day Candle") == "bdaycandle"
    assert short_collection_name("Astral Shard") == "astralshard"


def test_already_short_name_unchanged():
    """Готовое короткое имя не портится."""
    assert short_collection_name("lolpop") == "lolpop"


def test_empty_is_safe():
    """Пустое значение не роняет разбор."""
    assert short_collection_name("") == ""
    assert short_collection_name(None) == ""


@pytest.mark.asyncio
async def test_request_uses_short_name(monkeypatch):
    """В запрос уходит короткое имя, а не отображаемое."""
    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma x")
    seen = {}

    async def fake_request(method, path, **kwargs):
        """Запомнить параметры запроса."""
        seen.update(kwargs.get("params") or {})
        return {"collections": {"plushpepe": {"models": [], "symbols": [],
                                              "backdrops": []}}}

    monkeypatch.setattr(adapter, "request", fake_request)
    await adapter.attribute_floors("Plush Pepe")

    assert seen["short_names"] == "plushpepe"


@pytest.mark.asyncio
async def test_response_parsed_under_short_key(monkeypatch):
    """Ответ приходит под коротким ключом — по нему и разбираем.

    Раньше разбор искал ключ по отображаемому имени и на настоящем
    ответе не находил ничего.
    """
    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma x")

    async def fake_request(method, path, **kwargs):
        """Ответ в том виде, в каком его отдаёт площадка."""
        return {
            "collections": {
                "lolpop": {
                    "models": [
                        {"name": "Satellite", "floor_price": "30.69"},
                        {"name": "Angelina", "floor_price": "6.84"},
                    ],
                    "symbols": [],
                    "backdrops": [],
                }
            }
        }

    monkeypatch.setattr(adapter, "request", fake_request)
    floors = await adapter.attribute_floors("Lol Pop")

    assert floors["models"]["Satellite"] == Decimal("30.69")
    assert floors["models"]["Angelina"] == Decimal("6.84")


@pytest.mark.asyncio
async def test_empty_answer_gives_empty_floors(monkeypatch):
    """Пустой ответ — пустой результат, а не выдуманные цены."""
    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma x")

    async def fake_request(method, path, **kwargs):
        """Площадка не знает такой коллекции."""
        return {"collections": {"нечто": {"models": [], "symbols": [],
                                          "backdrops": []}}}

    monkeypatch.setattr(adapter, "request", fake_request)

    assert await adapter.attribute_floors("Нечто") == {
        "models": {}, "symbols": {}, "backdrops": {},
        "supply": {"models": {}, "symbols": {}, "backdrops": {}},
    }
