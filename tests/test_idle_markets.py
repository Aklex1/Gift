"""Тесты подсказки о неиспользуемых площадках.

Реальная жалоба: «бот не видит, что MRKT включена, хотя в веб-панели
она запущена». Бот был прав — стратегия перечисляла только telegram и
portals, — но узнать это из его карточки было нельзя.

Подключённая площадка и обходимая — разные вещи. Пока карточка
показывала одно и умалчивала о другом, расхождение выглядело поломкой
адаптера, а не незаполненной галочкой в стратегии.
"""

from __future__ import annotations

import pytest

from app.enums import Market
from app.services import strategy as strategy_service


@pytest.fixture()
def connected(monkeypatch):
    """Подключены три площадки."""
    monkeypatch.setattr(
        "app.adapters.registry.tradable_markets",
        lambda: [Market.TELEGRAM, Market.PORTALS, Market.MRKT],
    )


def test_unused_venue_is_named(connected):
    """Подключённая, но не выбранная площадка называется прямо."""
    assert strategy_service.idle_markets(["telegram", "portals"]) == ["mrkt"]


def test_nothing_idle_when_all_chosen(connected):
    """Когда обходятся все — подсказывать не о чем."""
    assert strategy_service.idle_markets(
        ["telegram", "portals", "mrkt"]
    ) == []


def test_empty_strategy_lists_everything(connected):
    """Стратегия без площадок не обходит ни одной — и это видно."""
    assert strategy_service.idle_markets([]) == ["telegram", "portals", "mrkt"]
    assert strategy_service.idle_markets(None) == [
        "telegram", "portals", "mrkt"
    ]


def test_case_and_spaces_do_not_hide_a_venue(connected):
    """«MRKT» с заглавными — та же площадка, а не забытая."""
    assert strategy_service.idle_markets([" Telegram ", "PORTALS", "MRKT"]) == []


def test_unconnected_venue_is_not_suggested(monkeypatch):
    """Площадка без токена в подсказку не идёт.

    Звать выбрать её значило бы предлагать то, что всё равно не
    заработает.
    """
    monkeypatch.setattr(
        "app.adapters.registry.tradable_markets",
        lambda: [Market.TELEGRAM, Market.PORTALS],
    )

    assert strategy_service.idle_markets(["telegram", "portals"]) == []
