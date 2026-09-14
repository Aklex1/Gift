"""Тесты уведомлений.

Главное требование: сообщать о проблеме один раз, а не каждые пять
минут. Спамящий бот перестают читать, и настоящая проблема теряется.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.services import notify, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Изолировать хранилище и перехватить отправку."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr("app.services.runtime._audit", lambda *a, **k: None)
    store.invalidate()
    yield
    store.invalidate()


@pytest.fixture()
def outbox(monkeypatch):
    """Собирать отправленные сообщения вместо реальной отправки."""
    sent: list[str] = []

    async def fake_send(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    return sent


# ----------------------------------------------------------------------
async def test_alert_is_sent_once(outbox, session):
    """Повтор о той же проблеме не приходит, пока идёт пауза."""
    for _ in range(5):
        await notify.alert("flood", "основной", "аккаунт на паузе")
    assert len(outbox) == 1


async def test_different_keys_are_independent(outbox, session):
    """Проблемы на разных аккаунтах сообщаются отдельно."""
    await notify.alert("flood", "первый", "пауза")
    await notify.alert("flood", "второй", "пауза")
    assert len(outbox) == 2


async def test_cooldown_expiry_allows_repeat(outbox, session):
    """После паузы напоминание приходит снова."""
    await notify.alert("flood", "основной", "пауза", cooldown=dt.timedelta(hours=1))
    assert len(outbox) == 1

    # Сдвигаем отметку времени назад, как будто пауза прошла.
    old = (dt.datetime.utcnow() - dt.timedelta(hours=2)).isoformat(timespec="seconds")
    store.set(notify._state_key("flood:основной"), old)

    await notify.alert("flood", "основной", "пауза", cooldown=dt.timedelta(hours=1))
    assert len(outbox) == 2


async def test_resolution_sent_once_and_resets(outbox, session):
    """О восстановлении сообщается один раз и пауза снимается."""
    await notify.alert("flood", "основной", "пауза")
    await notify.resolved("flood", "основной", "снова работает")
    assert len(outbox) == 2

    # Повтор без новой проблемы ничего не шлёт.
    await notify.resolved("flood", "основной", "снова работает")
    assert len(outbox) == 2

    # А новая проблема сообщается сразу, пауза была сброшена.
    await notify.alert("flood", "основной", "опять пауза")
    assert len(outbox) == 3


async def test_resolution_without_problem_is_silent(outbox, session):
    """Если о проблеме не сообщали, о её решении тоже молчим."""
    await notify.resolved("market", "portals", "снова отвечает")
    assert outbox == []


async def test_disabled_kind_is_not_sent(outbox, session):
    """Выключенный вид уведомлений не отправляется."""
    notify.set_enabled_kinds({"limit"})
    await notify.alert("flood", "основной", "пауза")
    assert outbox == []

    await notify.alert("limit", "total", "лимит исчерпан")
    assert len(outbox) == 1


async def test_all_kinds_have_titles():
    """У каждого вида есть человеческое название для интерфейса."""
    for key, title in notify.KINDS.items():
        assert title and title != key


async def test_defaults_are_subset_of_kinds():
    """Включённые по умолчанию виды существуют."""
    assert notify.DEFAULT_ENABLED <= set(notify.KINDS)


async def test_trade_notification_has_no_cooldown(outbox, session):
    """Каждая сделка сообщается отдельно: это не проблема, а событие."""
    from decimal import Decimal

    for i in range(3):
        await notify.notify_trade(
            ok=True, market="portals", name=f"Подарок #{i}",
            price=Decimal("3.9"), currency="TON",
        )
    assert len(outbox) == 3
