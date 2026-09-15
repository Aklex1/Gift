"""Тесты фильтров и порядка на странице кандидатов.

Список бывает длинным, а показывается сотня строк. Значит, порядок —
это не украшение: он решает, какие находки человек вообще увидит.
Поэтому проверяется не только «фильтр что-то отфильтровал», но и что
неизвестное не выдаётся за хорошее: кандидат без известного срока
продажи не должен всплывать наверх при сортировке по скорости и не
должен проходить фильтр «продаётся за три дня».
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest


@pytest.fixture()
def panel(session, monkeypatch):
    """Панель с набором кандидатов, различающихся показателями."""
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

    strategy = ss.create_strategy(session, name="s", budget_cap=Decimal("9000"))
    strategy.is_enabled = True

    def add(ident, name, market, price, fair, roi, gap, days, risk=20):
        gift = Gift(canonical_key=f"{name}#{ident}", collection=name, number=ident)
        session.add(gift)
        session.flush()
        session.add(
            Candidate(
                id=ident,
                strategy_id=strategy.id,
                gift_id=gift.id,
                market=market,
                listing_external_id=f"x{ident}",
                price_stars=Decimal(str(price)),
                fair_value_stars=Decimal(str(fair)),
                net_roi=Decimal(str(roi)),
                risk_score=risk,
                confidence="high",
                discount=None if gap is None else Decimal(str(gap)),
                days_to_sell=days,
                sale_velocity=(1.0 / days) if days else None,
                state="pending",
                rationale={},
                expires_at=utcnow() + dt.timedelta(minutes=10),
            )
        )

    # Большой разрыв, продаётся медленно.
    add(1, "Дорогой", "telegram", 100, 500, "0.30", "0.80", 25.0)
    # Разрыв поменьше, зато уходит за день.
    add(2, "Быстрый", "portals", 200, 400, "0.20", "0.50", 1.0)
    # Средний по всему.
    add(3, "Средний", "telegram", 300, 450, "0.10", "0.33", 8.0, risk=40)
    # Старый кандидат: показателей ещё нет.
    add(4, "Старый", "telegram", 400, 500, "0.25", None, None)
    session.flush()

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.web.server import app

    monkeypatch.setattr(settings, "web_password", "t")
    return TestClient(app)


AUTH = ("admin", "t")


def _names(panel, **params) -> list[str]:
    """Имена кандидатов в том порядке, в каком их показали."""
    import re

    page = panel.get("/candidates", params=params, auth=AUTH).text
    # Имя строки — описание подарка: «Дорогой #1» и так далее.
    return re.findall(r"<td>(Дорогой|Быстрый|Средний|Старый) #\d+</td>", page)


# --- порядок -----------------------------------------------------------


def test_default_order_is_by_gap(panel):
    """По умолчанию вперёд идёт самый большой разрыв.

    Именно им меряют удачную покупку, и именно его человек ищет
    глазами в первую очередь.
    """
    assert _names(panel)[:3] == ["Дорогой", "Быстрый", "Средний"]


def test_candidate_without_gap_goes_last(panel):
    """Кандидат без разрыва не всплывает наверх как нулевой.

    Показателя у него просто нет — это не повод ни прятать его, ни
    ставить впереди тех, у кого разрыв измерен.
    """
    assert _names(panel)[-1] == "Старый"


def test_order_by_roi(panel):
    """Порядок по ROI — прежнее поведение, оно никуда не делось."""
    assert _names(panel, sort="по ROI")[0] == "Дорогой"
    assert _names(panel, sort="по ROI")[1] == "Старый"


def test_order_by_speed_puts_the_quickest_first(panel):
    """По скорости вперёд выходит то, что уходит за день."""
    assert _names(panel, sort="по скорости продажи")[0] == "Быстрый"


def test_unknown_speed_does_not_look_fast(panel):
    """Неизвестный срок продажи — не «ноль дней», а конец списка."""
    assert _names(panel, sort="по скорости продажи")[-1] == "Старый"


def test_order_by_risk(panel):
    """По риску вперёд идут спокойные сделки."""
    assert _names(panel, sort="по риску")[-1] == "Средний"


def test_order_by_price(panel):
    """Порядок «сначала дешёвые» — по цене входа."""
    assert _names(panel, sort="сначала дешёвые") == [
        "Дорогой", "Быстрый", "Средний", "Старый"
    ]


# --- фильтры -----------------------------------------------------------


def test_filter_by_gap(panel):
    """Фильтр по разрыву оставляет только заметно дешёвые лоты."""
    assert _names(panel, gap="от 70%") == ["Дорогой"]


def test_gap_filter_drops_unmeasured(panel):
    """Без измеренного разрыва кандидат под фильтр не подходит.

    Пропустить его значило бы выдать незнание за большой разрыв.
    """
    assert "Старый" not in _names(panel, gap="от 20%")


def test_filter_by_speed(panel):
    """Фильтр по скорости оставляет то, что действительно уходит."""
    assert _names(panel, speed="до 3 дней") == ["Быстрый"]


def test_speed_filter_drops_unknown(panel):
    """Неизвестная скорость под фильтр «продаётся быстро» не проходит."""
    assert "Старый" not in _names(panel, speed="до 30 дней")


def test_filters_combine(panel):
    """Фильтры складываются, а не заменяют друг друга."""
    assert _names(panel, market="portals", gap="от 30%") == ["Быстрый"]


def test_market_filter_still_works(panel):
    """Фильтр по площадке не сломался о новые."""
    assert _names(panel, market="portals") == ["Быстрый"]


def test_unknown_filter_value_shows_everything(panel):
    """Незнакомое значение фильтра ничего не прячет молча."""
    assert len(_names(panel, gap="от 999%")) == 4


# --- фильтр переживает покупку ----------------------------------------


def test_filter_survives_the_buy_button(panel):
    """После нажатия «Купить» человек возвращается к своему списку.

    Иначе отобранная подборка молча рассыпается, и находку приходится
    искать заново.
    """
    response = panel.post(
        "/candidates/2/buy",
        data={"market": "portals", "gap": "от 30%", "sort": "по риску"},
        auth=AUTH,
        follow_redirects=False,
    )

    location = response.headers["location"]
    assert "market=portals" in location
    assert "gap=" in location
    assert "sort=" in location
