"""Тесты переноса подарка из панели.

Перенос — единственная необратимая операция бота: подарок уходит и не
возвращается ничем. Поэтому проверяется не только что он работает, но
и что случайно его не запустить.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Market, PositionStatus


@pytest.fixture()
def panel(session, monkeypatch):
    """Панель с одной открытой позицией в Telegram."""
    from contextlib import contextmanager

    import app.db as db_module
    from app.models import Gift, Position, utcnow
    from app.services import store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr("app.web.server.session_scope", scope)
    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()

    gift = Gift(canonical_key="lol pop#1", collection="Lol Pop", number=1,
                slug="lolpop-1")
    session.add(gift)
    session.flush()
    session.add(
        Position(
            id=1, gift_id=gift.id, status=PositionStatus.HELD,
            custody_market=Market.TELEGRAM, buy_market=Market.TELEGRAM,
            buy_price=Decimal("450"), bought_at=utcnow(),
        )
    )
    session.flush()

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.web.server import app

    monkeypatch.setattr(settings, "web_password", "t")
    yield TestClient(app)
    store.invalidate()


AUTH = ("admin", "t")


def test_button_hidden_while_transfer_disabled(panel):
    """Пока перенос выключен, кнопки нет — только пояснение."""
    page = panel.get("/portfolio", auth=AUTH).text

    assert "перенос выключен" in page
    assert "На Portals" not in page


def test_button_appears_when_enabled(panel):
    """После включения кнопка появляется."""
    from app.services import runtime

    runtime.set_transfer_enabled(True)
    try:
        page = panel.get("/portfolio", auth=AUTH).text
    finally:
        runtime.set_transfer_enabled(False)

    assert "На Portals" in page


def test_first_click_only_warns(panel, session):
    """Первое нажатие ничего не передаёт."""
    from app.models import Position

    response = panel.post("/positions/1/transfer", auth=AUTH, follow_redirects=False)

    assert response.headers["location"] == "/portfolio?confirm=1"
    assert session.get(Position, 1).custody_market is Market.TELEGRAM


def test_warning_says_it_is_irreversible(panel):
    """На втором экране сказано главное: вернуть нельзя."""
    page = panel.get("/portfolio?confirm=1", auth=AUTH).text

    assert "вернуть его будет нельзя" in page
    assert "transfer-target" in page


def test_confirmed_transfer_calls_executor(panel, monkeypatch):
    """Подтверждение доходит до исполнителя."""
    from app.services import executor

    called = {}

    async def fake(position_id, *, actor, to_market=Market.PORTALS):
        """Подменённый перенос."""
        called.update(id=position_id, actor=actor)
        return {"ok": True, "detail": "подарок передан"}

    monkeypatch.setattr(executor, "execute_transfer", fake)
    response = panel.post(
        "/positions/1/transfer", auth=AUTH, data={"confirmed": "1"},
        follow_redirects=False,
    )

    assert called == {"id": 1, "actor": "web"}
    assert "saved=" in response.headers["location"]


def test_refusal_is_shown(panel, monkeypatch):
    """Отказ исполнителя виден пользователю."""
    from urllib.parse import unquote

    from app.services import executor

    async def fake(position_id, *, actor, to_market=Market.PORTALS):
        """Перенос выключен в настройках."""
        return {"ok": False, "detail": "перенос подарков выключен"}

    monkeypatch.setattr(executor, "execute_transfer", fake)
    response = panel.post(
        "/positions/1/transfer", auth=AUTH, data={"confirmed": "1"},
        follow_redirects=False,
    )

    assert "выключен" in unquote(response.headers["location"])


def test_toggle_persists(panel):
    """Переключатель на странице «Торговля» сохраняется."""
    from app.services import runtime, store

    panel.post("/trading/transfer", auth=AUTH, data={"enabled": "on"},
               follow_redirects=False)
    store.invalidate()
    assert runtime.transfer_enabled() is True

    panel.post("/trading/transfer", auth=AUTH, follow_redirects=False)
    store.invalidate()
    assert runtime.transfer_enabled() is False


def test_listed_position_cannot_be_transferred(panel, session):
    """Выставленный лот переносить нельзя — сначала снять с продажи."""
    from app.models import Position
    from app.services import runtime

    position = session.get(Position, 1)
    position.list_external_id = "x"
    session.flush()

    runtime.set_transfer_enabled(True)
    try:
        page = panel.get("/portfolio", auth=AUTH).text
    finally:
        runtime.set_transfer_enabled(False)

    assert "На Portals" not in page


def test_cooldown_shown(panel, session):
    """Пока идёт cooldown, показан срок, а не кнопка."""
    from app.models import Position, utcnow
    from app.services import runtime

    position = session.get(Position, 1)
    position.resale_available_at = utcnow() + dt.timedelta(hours=5)
    session.flush()

    runtime.set_transfer_enabled(True)
    try:
        page = panel.get("/portfolio", auth=AUTH).text
    finally:
        runtime.set_transfer_enabled(False)

    assert "На Portals" not in page
    assert "до " in page


# --- что будет с позицией дальше --------------------------------------


def _step(session, **changes):
    """Пояснение для позиции с заданными изменениями."""
    from app.models import Position
    from app.web.server import _next_step

    position = session.get(Position, 1)
    for key, value in changes.items():
        setattr(position, key, value)
    session.flush()
    return _next_step(position)


def test_held_in_safe_mode_will_not_list(panel, session):
    """В SAFE подарок не выставится, и сказано почему."""
    step = _step(session)

    assert "SAFE" in step
    assert "не выставится" in step


def test_held_with_market_off(panel, session):
    """В SEMI, но с выключенной площадкой — причина другая."""
    from app.enums import TradeMode
    from app.services import runtime, store

    runtime.set_mode(TradeMode.SEMI)
    store.invalidate()
    try:
        step = _step(session)
    finally:
        runtime.set_mode(TradeMode.SAFE)
        store.invalidate()

    assert "боевой режим" in step
    assert "telegram" in step


def test_held_ready_to_list(panel, session):
    """Когда всё включено — сказано, что выставится само."""
    from app.enums import Market, TradeMode
    from app.services import runtime, store

    runtime.set_mode(TradeMode.SEMI)
    runtime.set_write_enabled(Market.TELEGRAM, True)
    store.invalidate()
    try:
        step = _step(session)
    finally:
        runtime.set_mode(TradeMode.SAFE)
        runtime.set_write_enabled(Market.TELEGRAM, False)
        store.invalidate()

    assert "будет выставлено автоматически" in step


def test_cooldown_beats_other_reasons(panel, session):
    """Пока идёт cooldown, называется он, а не режим.

    Иначе человек пойдёт включать боевой режим, хотя дело не в нём.
    """
    from app.models import utcnow

    step = _step(session, resale_available_at=utcnow() + dt.timedelta(hours=3))

    assert "cooldown" in step


def test_listed_position_explained(panel, session):
    """Выставленная позиция объясняет, что с ней происходит теперь."""
    from app.enums import PositionStatus

    step = _step(session, status=PositionStatus.LISTED)

    assert "выставлено" in step
    assert "репрайсер" in step


def test_column_visible_in_panel(panel):
    """Пояснение видно на странице, а не только в коде."""
    page = panel.get("/portfolio", auth=AUTH).text

    assert "Что дальше" in page
    assert "не выставится" in page
