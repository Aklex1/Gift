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
    saved = (settings.kill_switch, settings.auto_whitelist)
    yield
    settings.kill_switch, settings.auto_whitelist = saved


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


def test_semi_mode_allows_official_market():
    """SEMI разрешает покупку на официальном API."""
    settings.kill_switch = False
    guard(TradeMode.SEMI, Market.TELEGRAM, Capability.BUY)


def test_private_market_buy_is_unavailable():
    """Покупка на приватных площадках закрыта на уровне адаптера."""
    settings.kill_switch = False
    for market in (Market.PORTALS, Market.MRKT, Market.TONNEL, Market.GETGEMS):
        with pytest.raises(ExecutionBlocked, match="недоступна"):
            guard(TradeMode.SEMI, market, Capability.BUY)


def test_auto_requires_whitelist():
    """AUTO не работает без явного белого списка."""
    settings.kill_switch = False
    settings.auto_whitelist = ""
    with pytest.raises(ExecutionBlocked, match="AUTO_WHITELIST"):
        guard(TradeMode.AUTO, Market.TELEGRAM, Capability.BUY)


def test_auto_allowed_for_whitelisted_official_market():
    """AUTO разрешён только для официального API из белого списка."""
    settings.kill_switch = False
    settings.auto_whitelist = "telegram"
    guard(TradeMode.AUTO, Market.TELEGRAM, Capability.BUY)


def test_auto_never_allowed_for_private_api():
    """Приватный API нельзя пустить в AUTO даже через белый список."""
    settings.kill_switch = False
    settings.auto_whitelist = "portals,mrkt,tonnel"
    for market in (Market.PORTALS, Market.MRKT, Market.TONNEL):
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
