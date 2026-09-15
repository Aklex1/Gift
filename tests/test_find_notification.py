"""Тесты немедленной отправки находки.

Смысл уведомления целиком в слове «сразу». Недооценённый лот живёт
минуты: находка, которую владелец увидит вечером в таблице, — это уже
не находка, а отчёт о чужой покупке.

Второе, что здесь проверяется, — пауза. Кандидат протухает и находится
снова; без паузы один и тот же лот присылал бы карточку каждые
несколько минут, и читать их перестали бы все разом, вместе с важными.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services import notify, store


@pytest.fixture(autouse=True)
def sent(session, monkeypatch):
    """Перехваченные отправки вместо настоящих."""
    from contextlib import contextmanager

    import app.db as db_module

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()

    out: list[str] = []

    async def fake_send(text):
        out.append(text)
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    yield out
    store.invalidate()


@pytest.mark.asyncio
async def test_find_is_delivered(sent):
    """Карточка уходит владельцу."""
    assert await notify.found("mrkt", "lot-1", "карточка")

    assert sent == ["карточка"]


@pytest.mark.asyncio
async def test_same_lot_is_not_repeated(sent):
    """Тот же лот второй раз не присылается."""
    await notify.found("mrkt", "lot-1", "карточка")
    await notify.found("mrkt", "lot-1", "карточка снова")

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_another_lot_is_its_own_find(sent):
    """Пауза считается по лоту, а не по всем находкам сразу."""
    await notify.found("mrkt", "lot-1", "первая")
    await notify.found("mrkt", "lot-2", "вторая")

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_same_id_on_another_venue_is_a_different_lot(sent):
    """Совпадение идентификаторов между площадками — не повтор."""
    await notify.found("mrkt", "1", "на mrkt")
    await notify.found("portals", "1", "на portals")

    assert len(sent) == 2


@pytest.mark.asyncio
async def test_finds_can_be_switched_off(sent):
    """Владелец может выключить находки, не выключая тревоги."""
    notify.set_enabled_kinds({"scan", "trade"})

    assert not await notify.found("mrkt", "lot-1", "карточка")
    assert sent == []


def test_finds_are_on_by_default():
    """Без настройки находки приходят: ради них бот и работает."""
    assert "find" in notify.DEFAULT_ENABLED
