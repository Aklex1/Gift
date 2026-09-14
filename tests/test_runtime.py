"""Тесты общих торговых переключателей.

Главное требование: переключатель, изменённый в одном процессе,
действует в остальных. Иначе аварийный стоп из панели не остановит
торгующий воркер — а это прямая потеря денег.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import settings
from app.enums import Market, TradeMode
from app.services import runtime, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Подменить БД приложения на тестовую и сбросить кэш."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr(runtime, "store", store)
    monkeypatch.setattr("app.services.runtime._audit", lambda *a, **k: None)
    store.invalidate()
    yield
    store.invalidate()


def _other_process_view() -> None:
    """Сымитировать другой процесс: своя память, общая БД."""
    store.invalidate()


# ----------------------------------------------------------------------
def test_kill_switch_reaches_other_processes(session):
    """Стоп, нажатый в панели, виден воркеру.

    Раньше он менял объект настроек только в своём процессе, и воркер
    продолжал торговать.
    """
    assert runtime.kill_switch() is False

    runtime.set_kill_switch(True, actor="web")
    _other_process_view()

    assert runtime.kill_switch() is True


def test_kill_switch_can_be_lifted(session):
    """Стоп снимается так же централизованно."""
    runtime.set_kill_switch(True)
    _other_process_view()
    runtime.set_kill_switch(False)
    _other_process_view()
    assert runtime.kill_switch() is False


def test_mode_is_shared(session):
    """Режим торговли общий для всех процессов."""
    runtime.set_mode(TradeMode.SEMI)
    _other_process_view()
    assert runtime.mode() is TradeMode.SEMI


def test_market_switch_is_shared(session):
    """Боевой режим площадки виден всем процессам."""
    assert runtime.write_enabled(Market.PORTALS) is False

    runtime.set_write_enabled(Market.PORTALS, True)
    _other_process_view()

    assert runtime.write_enabled(Market.PORTALS) is True


def test_every_market_is_off_by_default(session, monkeypatch):
    """Без явного включения не торгует ни одна площадка."""
    monkeypatch.setattr(settings, "telegram_enable_write", False)
    monkeypatch.setattr(settings, "portals_enable_write", False)
    monkeypatch.setattr(settings, "mrkt_enable_write", False)
    for market in runtime.TRADABLE:
        assert runtime.write_enabled(market) is False
    assert runtime.auto_markets() == set()


def test_trade_cap_stored_in_market_currency(session):
    """Лимит хранится в валюте площадки."""
    runtime.set_trade_cap(Market.PORTALS, Decimal("2.5"))
    runtime.set_trade_cap(Market.TELEGRAM, Decimal("5000"))
    _other_process_view()

    assert runtime.trade_cap(Market.PORTALS) == Decimal("2.5")
    assert runtime.trade_cap(Market.TELEGRAM) == Decimal("5000")
    assert runtime.cap_currency(Market.PORTALS).value == "TON"
    assert runtime.cap_currency(Market.TELEGRAM).value == "STARS"


def test_zero_cap_means_not_set(session):
    """Нулевой лимит считается незаданным."""
    runtime.set_trade_cap(Market.PORTALS, Decimal("0"))
    _other_process_view()
    assert runtime.trade_cap(Market.PORTALS) is None


def test_auto_markets_follow_switches(session):
    """Автономная торговля идёт только по включённым площадкам."""
    runtime.set_write_enabled(Market.TELEGRAM, True)
    runtime.set_write_enabled(Market.PORTALS, False)
    _other_process_view()

    assert runtime.auto_markets() == {"telegram"}


def test_env_used_until_panel_sets_value(session, monkeypatch):
    """Пока в панели не трогали, действует значение из .env."""
    monkeypatch.setattr(settings, "portals_enable_write", True)
    assert runtime.write_enabled(Market.PORTALS) is True

    # Выключение из панели перекрывает .env.
    runtime.set_write_enabled(Market.PORTALS, False)
    _other_process_view()
    assert runtime.write_enabled(Market.PORTALS) is False


def test_unknown_mode_falls_back_to_env(session, monkeypatch):
    """Испорченное значение режима не ломает бота."""
    store.set(runtime.KEY_MODE, "чепуха")
    monkeypatch.setattr(settings, "default_mode", TradeMode.SAFE)
    _other_process_view()
    assert runtime.mode() is TradeMode.SAFE
