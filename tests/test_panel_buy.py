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


def test_truncation_is_disclosed(panel, session):
    """Если кандидатов больше, чем помещается, об этом сказано прямо.

    Молча показывать часть — значит скрывать находки, о существовании
    которых никто не узнает.
    """
    import datetime as dt

    from app.models import Candidate, Gift, utcnow
    from app.web import server

    # Делаем список заведомо длиннее предела показа.
    server.CANDIDATES_SHOWN = 2
    try:
        for i in range(5):
            gift = Gift(canonical_key=f"c#{i}", collection="C", number=i)
            session.add(gift)
            session.flush()
            session.add(
                Candidate(
                    strategy_id=1, gift_id=gift.id, market="telegram",
                    listing_external_id=str(i), price_stars=Decimal("100"),
                    fair_value_stars=Decimal("200"), net_roi=Decimal("0.1"),
                    risk_score=10, confidence="high", state="pending",
                    expires_at=utcnow() + dt.timedelta(minutes=10),
                )
            )
        session.flush()

        page = panel.get("/candidates", auth=AUTH).text
    finally:
        server.CANDIDATES_SHOWN = 100

    assert "Показаны <b>2</b>" in page
    assert "<b>6</b>" in page
    assert "никуда" in page and "делись" in page


def test_full_list_says_so(panel):
    """Когда влезают все — так и написано, без лишней тревоги."""
    page = panel.get("/candidates", auth=AUTH).text

    assert "Показаны все" in page


# --- фильтры на странице кандидатов -----------------------------------


@pytest.fixture()
def many(panel, session):
    """Кандидаты на разных площадках и с разным ROI."""
    import datetime as dt

    from app.models import Candidate, Gift, utcnow

    rows = [
        ("telegram", "0.03"), ("telegram", "0.12"),
        ("portals", "0.07"), ("portals", "0.35"), ("portals", "1.20"),
    ]
    for i, (market, roi) in enumerate(rows, start=10):
        gift = Gift(canonical_key=f"g#{i}", collection="C", number=i)
        session.add(gift)
        session.flush()
        session.add(
            Candidate(
                strategy_id=1, gift_id=gift.id, market=market,
                listing_external_id=str(i), price_stars=Decimal("100"),
                fair_value_stars=Decimal("200"), net_roi=Decimal(roi),
                risk_score=10, confidence="high", state="pending",
                expires_at=utcnow() + dt.timedelta(minutes=10),
            )
        )
    session.flush()
    return panel


def test_filter_by_market(many):
    """Выбор площадки оставляет только её кандидатов."""
    page = many.get("/candidates?market=portals", auth=AUTH).text

    # Три лота Portals и ни одного телеграмного (кроме заголовков).
    assert page.count("<td>portals</td>") == 3
    assert "<td>telegram</td>" not in page


def test_filter_by_roi(many):
    """Порог ROI отсекает всё, что ниже."""
    page = many.get("/candidates?roi=от+20%25", auth=AUTH).text

    # Проходят только 35% и 120%.
    assert "35.0%" in page
    assert "120.0%" in page
    assert "3.0%" not in page


def test_filters_combine(many):
    """Площадка и ROI действуют вместе."""
    page = many.get("/candidates?market=portals&roi=от+50%25", auth=AUTH).text

    assert "120.0%" in page
    assert "35.0%" not in page


def test_filter_reports_hidden_total(many):
    """Видно, сколько кандидатов скрыл фильтр, а не только сколько прошло."""
    page = many.get("/candidates?market=portals", auth=AUTH).text

    assert "всего кандидатов" in page


def test_empty_filter_result_explains(many):
    """Пустая выборка объясняется и предлагает сбросить фильтр."""
    page = many.get("/candidates?roi=от+100%25&market=telegram", auth=AUTH).text

    assert "не подошёл ни один" in page
    assert "Сбросить фильтр" in page


def test_filter_survives_buy_click(many, session):
    """Фильтр не слетает при нажатии «Купить»."""
    response = many.post(
        "/candidates/1/buy", auth=AUTH,
        data={"market": "portals", "roi": "от 20%"},
        follow_redirects=False,
    )

    location = response.headers["location"]
    assert "market=portals" in location
    assert "confirm=1" in location


def test_no_filter_shows_everything(many):
    """Без фильтра видны все площадки."""
    page = many.get("/candidates", auth=AUTH).text

    assert "<td>portals</td>" in page
    assert "<td>telegram</td>" in page
