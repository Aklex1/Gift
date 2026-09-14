"""Тесты предохранителей исполнения.

Проверяется главное: система не может потратить деньги там, где
ей это не разрешено.
"""

from __future__ import annotations

import pytest

from app.adapters.base import Capability, CapabilityStatus
from app.adapters.registry import get_adapter
from app.config import settings
from app.enums import Market, TradeMode
from app.services.executor import ExecutionBlocked, guard


@pytest.fixture(autouse=True)
def restore_settings():
    """Вернуть настройки после каждого теста."""
    from app.services import store

    saved = (settings.kill_switch, settings.telegram_enable_write)
    store.invalidate()
    yield
    settings.kill_switch, settings.telegram_enable_write = saved
    store.invalidate()


def test_kill_switch_blocks_everything():
    """Аварийный стоп запрещает любую операцию."""
    settings.kill_switch = True
    with pytest.raises(ExecutionBlocked, match="аварийный стоп|KILL_SWITCH"):
        guard(TradeMode.SEMI, Market.TELEGRAM, Capability.BUY)


def test_safe_mode_blocks_writes():
    """В SAFE система только рекомендует."""
    settings.kill_switch = False
    with pytest.raises(ExecutionBlocked, match="SAFE"):
        guard(TradeMode.SAFE, Market.TELEGRAM, Capability.BUY)


def test_semi_mode_allows_enabled_official_market(monkeypatch):
    """SEMI разрешает покупку на включённом официальном API."""
    settings.kill_switch = False
    monkeypatch.setattr(settings, "telegram_enable_write", True)
    guard(TradeMode.SEMI, Market.TELEGRAM, Capability.BUY)


def test_market_off_by_default_blocks_trade():
    """Пока площадка не включена, торговли нет даже в SEMI."""
    settings.kill_switch = False
    with pytest.raises(ExecutionBlocked, match="боевой режим выключен"):
        guard(TradeMode.SEMI, Market.TELEGRAM, Capability.BUY)


def test_private_market_buy_blocked_by_default():
    """По умолчанию покупка на приватных площадках запрещена.

    Боевой режим каждой площадки включается отдельным флагом, и даже
    после этого нужен файл контракта с описанием эндпоинтов.
    """
    settings.kill_switch = False
    for market in (Market.PORTALS, Market.MRKT, Market.TONNEL, Market.GETGEMS):
        with pytest.raises(ExecutionBlocked):
            guard(TradeMode.SEMI, market, Capability.BUY)


def test_write_flag_alone_does_not_open_buy(monkeypatch):
    """Одного флага мало: без контракта эндпоинтов покупки нет."""
    from app.adapters import registry

    settings.kill_switch = False
    monkeypatch.setattr(settings, "portals_enable_write", True)
    registry._ADAPTERS.clear()
    try:
        with pytest.raises(ExecutionBlocked, match="недоступна"):
            guard(TradeMode.SEMI, Market.PORTALS, Capability.BUY)
    finally:
        registry._ADAPTERS.clear()


def test_auto_requires_market_enabled():
    """AUTO не работает на выключенной площадке."""
    settings.kill_switch = False
    with pytest.raises(ExecutionBlocked, match="боевой режим выключен"):
        guard(TradeMode.AUTO, Market.TELEGRAM, Capability.BUY)


def test_auto_allowed_for_enabled_official_market(monkeypatch):
    """AUTO разрешён на включённом официальном API."""
    settings.kill_switch = False
    monkeypatch.setattr(settings, "telegram_enable_write", True)
    guard(TradeMode.AUTO, Market.TELEGRAM, Capability.BUY)


def test_auto_never_allowed_for_private_api_by_default(monkeypatch):
    """Приватный API не уходит в AUTO без отдельного разрешения."""
    settings.kill_switch = False
    monkeypatch.setattr(settings, "portals_enable_write", True)
    monkeypatch.setattr(settings, "mrkt_enable_write", True)
    monkeypatch.setattr(settings, "allow_experimental_auto", False)
    for market in (Market.PORTALS, Market.MRKT):
        with pytest.raises(ExecutionBlocked):
            guard(TradeMode.AUTO, market, Capability.BUY)


def test_only_telegram_is_supported_for_buy():
    """Статус supported для покупки есть только у Telegram."""
    assert (
        get_adapter(Market.TELEGRAM).status_of(Capability.BUY)
        is CapabilityStatus.SUPPORTED
    )
    for market in (Market.PORTALS, Market.MRKT, Market.TONNEL, Market.GETGEMS):
        assert (
            get_adapter(market).status_of(Capability.BUY)
            is CapabilityStatus.UNAVAILABLE
        )
