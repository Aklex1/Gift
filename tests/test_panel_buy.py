"""Тесты подтверждения покупки в веб-панели.

Покупка необратима, поэтому нажатие в таблице не должно тратить
деньги: сначала показывается сумма списания, и только отдельное
подтверждение запускает исполнение — так же, как в Telegram-боте.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest


@pytest.fixture()
def panel(session, monkeypatch, tmp_path):
    """Панель с одним кандидатом и подменённой БД."""
    from contextlib import contextmanager

    import app.db as db_module
    from app.models import Candidate, Gift, utcnow
    from app.services import strategy as ss

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr("app.web.server.session_scope", scope)

    strategy = ss.create_strategy(session, name="s", budget_cap=Decimal("2000"))
    strategy.is_enabled = True
    gift = Gift(canonical_key="chill flame#1", collection="Chill Flame", number=1)
    session.add(gift)
    session.flush()
    session.add(
        Candidate(
            id=1,
            strategy_id=strategy.id,
            gift_id=gift.id,
            market="telegram",
            listing_external_id="x",
            price_stars=Decimal("450"),
            fair_value_stars=Decimal("1061"),
            net_roi=Decimal("0.082"),
            risk_score=20,
            confidence="high",
            state="pending",
            rationale={"expected_sale_price": "609"},
            expires_at=utcnow() + dt.timedelta(minutes=10),
        )
    )
    session.flush()

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.web.server import app

    monkeypatch.setattr(settings, "web_password", "t")
    return TestClient(app)


AUTH = ("admin", "t")


def test_expected_sale_price_shown(panel):
    """Рядом с «Оценкой» видно цену, от которой на деле считался ROI.

    Без неё ROI 8% рядом с оценкой 1061 ★ при цене 450 ★ выглядит
    ошибкой: расчёт идёт по floor, а он ниже медианы.
    """
    page = panel.get("/candidates", auth=AUTH).text

    assert "Продажа по" in page
    assert "609" in page


def test_first_click_does_not_buy(panel, session):
    """Первое нажатие только разворачивает предупреждение."""
    from app.models import Candidate

    response = panel.post("/candidates/1/buy", auth=AUTH, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/candidates?confirm=1"
    assert session.get(Candidate, 1).state == "pending"


def test_warning_explains_irreversibility(panel):
    """На втором экране сказано, что операция необратима и сколько спишут."""
    page = panel.get("/candidates?confirm=1", auth=AUTH).text

    assert "необратима" in page
    assert "450" in page
    assert "Подтвердить покупку" in page


def test_confirmed_purchase_is_attempted_and_audited(panel, session, monkeypatch):
    """Подтверждение запускает исполнение и пишет результат в аудит."""
    from app.models import AuditLog
    from app.services import executor

    called = {}

    async def fake_buy(candidate_id, *, actor, mode):
        """Подменённое исполнение: проверяем, что дошли до него."""
        called.update(id=candidate_id, actor=actor, mode=mode)
        return {"ok": True, "detail": "куплено за 450 STARS"}

    monkeypatch.setattr(executor, "execute_buy", fake_buy)

    response = panel.post(
        "/candidates/1/buy", auth=AUTH, data={"confirmed": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert called["id"] == 1
    assert called["actor"] == "web"

    entry = session.query(AuditLog).filter_by(action="candidate.buy").one()
    assert entry.ok is True
    assert entry.target == "1"


def test_failure_is_reported_and_audited(panel, session, monkeypatch):
    """Отказ площадки виден пользователю и попадает в аудит."""
    from app.models import AuditLog
    from app.services import executor

    async def fake_buy(candidate_id, *, actor, mode):
        """Площадка отказала."""
        return {"ok": False, "detail": "боевой режим выключен"}

    monkeypatch.setattr(executor, "execute_buy", fake_buy)

    response = panel.post(
        "/candidates/1/buy", auth=AUTH, data={"confirmed": "1"},
        follow_redirects=False,
    )

    assert "saved=" in response.headers["location"]
    assert session.query(AuditLog).filter_by(action="candidate.buy").one().ok is False


def test_unknown_outcome_warns_against_retry(panel, monkeypatch):
    """Неизвестный исход не предлагает повторить: деньги могли уйти."""
    from app.services import executor

    async def fake_buy(candidate_id, *, actor, mode):
        """Связь оборвалась после отправки."""
        return {"ok": None, "detail": "связь оборвалась"}

    monkeypatch.setattr(executor, "execute_buy", fake_buy)

    response = panel.post(
        "/candidates/1/buy", auth=AUTH, data={"confirmed": "1"},
        follow_redirects=False,
    )
    location = response.headers["location"]

    from urllib.parse import unquote

    assert "повторная покупка не выполняется" in unquote(location)


def test_buy_requires_auth(panel):
    """Без пароля панель покупку не примет."""
    assert panel.post("/candidates/1/buy", data={"confirmed": "1"}).status_code == 401


# --- банер про стратегии ----------------------------------------------


def test_enabled_strategy_shown_even_while_scanning(panel, session):
    """Пока идёт проход, панель не должна уверять, что стратегий нет.

    Раньше вопрос «включена ли стратегия» задавался отчёту сканера.
    Отчёт во время прохода содержит только пометку о начале, и панель
    сообщала, что стратегий нет, хотя они работали.
    """
    from app.services import scanner

    # Отчёт ровно такой, каким он бывает в середине прохода.
    session.flush()
    original = scanner.last_report
    scanner.last_report = lambda: {"running": True, "started_at": "2026-09-15T03:19:38"}
    try:
        page = panel.get("/candidates", auth=AUTH).text
    finally:
        scanner.last_report = original

    assert "Ни одна стратегия не включена" not in page
    assert "включённые стратегии" in page


def test_missing_report_does_not_hide_strategies(panel, session):
    """И когда сканер ещё ни разу не отработал — тоже."""
    from app.services import scanner

    original = scanner.last_report
    scanner.last_report = lambda: None
    try:
        page = panel.get("/candidates", auth=AUTH).text
    finally:
        scanner.last_report = original

    assert "Ни одна стратегия не включена" not in page


def test_banner_appears_when_really_disabled(panel, session):
    """А когда стратегий правда нет — предупреждение на месте."""
    from app.models import Strategy

    session.query(Strategy).update({"is_enabled": False})
    session.flush()

    page = panel.get("/candidates", auth=AUTH).text

    assert "Ни одна стратегия не включена" in page
