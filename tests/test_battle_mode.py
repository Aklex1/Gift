"""Тесты боевого режима приватных площадок.

Проверяется главное: деньги нельзя потратить, пока владелец
не сделал два независимых явных действия — включил флаг площадки
и описал эндпоинты в файле контракта.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.adapters.base import Capability, CapabilityStatus
from app.adapters.contracts import Endpoint, load, write_template
from app.config import settings
from app.enums import Currency, Market, TradeMode
from app.services.executor import ExecutionBlocked, guard


@pytest.fixture()
def contracts_dir(tmp_path, monkeypatch):
    """Изолированный каталог контрактов."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / "markets").mkdir(parents=True, exist_ok=True)
    return tmp_path / "markets"


@pytest.fixture(autouse=True)
def clean_registry():
    """Сбрасывать кэш адаптеров: конфиг меняется между тестами."""
    from app.adapters import registry

    registry._ADAPTERS.clear()
    yield
    registry._ADAPTERS.clear()


@pytest.fixture(autouse=True)
def restore_settings():
    """Вернуть флаги после теста."""
    saved = (
        settings.kill_switch,
        settings.portals_enable_write,
        settings.portals_auth,
        settings.allow_experimental_auto,
        settings.auto_whitelist,
        settings.portals_max_trade_ton,
    )
    settings.kill_switch = False
    yield
    (
        settings.kill_switch,
        settings.portals_enable_write,
        settings.portals_auth,
        settings.allow_experimental_auto,
        settings.auto_whitelist,
        settings.portals_max_trade_ton,
    ) = saved


# ----------------------------------------------------------------------
# Контракт
# ----------------------------------------------------------------------
def test_missing_contract_means_no_write(contracts_dir):
    """Без файла контракта боевых операций нет."""
    contract = load("portals")
    assert contract.described == []
    assert not contract.has("buy")


def test_contract_opens_only_described_operations(contracts_dir):
    """Открываются ровно те операции, что описаны."""
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    contract = load("portals")
    assert contract.has("buy")
    assert not contract.has("cancel"), "недописанная операция не должна открываться"


def test_endpoint_substitutes_price_and_id():
    """Подстановки в пути и теле работают."""
    endpoint = Endpoint(
        "test.buy",
        {
            "method": "POST",
            "path": "/nfts/{external_id}/buy",
            "body": {"price": "{price}", "nano": "{price_nano}"},
        },
    )
    method, path, kwargs = endpoint.render(
        external_id="abc123", price=Decimal("2.5")
    )
    assert method == "POST"
    assert path == "/nfts/abc123/buy"
    assert kwargs["json"]["price"] == "2.5"
    assert kwargs["json"]["nano"] == "2500000000"


def test_success_rule_decides_outcome():
    """Правило успеха разбирает ответ площадки."""
    endpoint = Endpoint(
        "test.buy",
        {
            "method": "POST",
            "path": "/buy",
            "success_when": {"field": "result.status", "equals": "ok"},
        },
    )
    assert endpoint.succeeded({"result": {"status": "ok"}}) is True
    assert endpoint.succeeded({"result": {"status": "failed"}}) is False
    # Поля нет — судить нельзя, исход неизвестен.
    assert endpoint.succeeded({"other": 1}) is None


def test_template_is_not_usable_as_is(contracts_dir):
    """Заготовка контракта не должна случайно уйти в бой."""
    from app.adapters.contracts import is_placeholder

    write_template("portals")
    assert is_placeholder("portals")
    # И, главное, заглушка не открывает ни одной боевой операции.
    assert load("portals").described == []


# ----------------------------------------------------------------------
# Предохранители
# ----------------------------------------------------------------------
def test_flag_without_contract_keeps_buy_closed(contracts_dir):
    """Флаг без контракта не открывает покупку."""
    settings.portals_enable_write = True
    settings.portals_auth = "token"
    with pytest.raises(ExecutionBlocked, match="недоступна"):
        guard(TradeMode.SEMI, Market.PORTALS, Capability.BUY)


def test_contract_without_flag_keeps_buy_closed(contracts_dir):
    """Контракт без флага не открывает покупку."""
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    settings.portals_enable_write = False
    settings.portals_auth = "token"
    with pytest.raises(ExecutionBlocked, match="боевой режим выключен"):
        guard(TradeMode.SEMI, Market.PORTALS, Capability.BUY)


def test_flag_and_contract_together_open_semi(contracts_dir):
    """Флаг плюс контракт открывают покупку с подтверждением."""
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    settings.portals_enable_write = True
    settings.portals_auth = "token"
    guard(TradeMode.SEMI, Market.PORTALS, Capability.BUY)

    from app.adapters.registry import get_adapter

    adapter = get_adapter(Market.PORTALS)
    assert adapter.status_of(Capability.BUY) is CapabilityStatus.EXPERIMENTAL


def test_auto_still_blocked_without_experimental_flag(contracts_dir):
    """Приватная площадка не уходит в AUTO без отдельного разрешения.

    Даже при включённом боевом режиме и белом списке.
    """
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    settings.portals_enable_write = True
    settings.portals_auth = "token"
    settings.auto_whitelist = "portals"
    settings.allow_experimental_auto = False
    with pytest.raises(ExecutionBlocked, match="ALLOW_EXPERIMENTAL_AUTO"):
        guard(TradeMode.AUTO, Market.PORTALS, Capability.BUY)


def test_auto_allowed_with_all_three_switches(contracts_dir):
    """Автономная торговля требует трёх независимых разрешений."""
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    settings.portals_enable_write = True
    settings.portals_auth = "token"
    settings.auto_whitelist = "portals"
    settings.allow_experimental_auto = True
    guard(TradeMode.AUTO, Market.PORTALS, Capability.BUY)


def test_kill_switch_overrides_battle_mode(contracts_dir):
    """Аварийный стоп сильнее любых разрешений."""
    (contracts_dir / "portals.json").write_text(
        json.dumps({"buy": {"method": "POST", "path": "/nfts/buy"}}),
        encoding="utf-8",
    )
    settings.portals_enable_write = True
    settings.portals_auth = "token"
    settings.auto_whitelist = "portals"
    settings.allow_experimental_auto = True
    settings.kill_switch = True
    with pytest.raises(ExecutionBlocked, match="аварийный стоп|KILL_SWITCH"):
        guard(TradeMode.AUTO, Market.PORTALS, Capability.BUY)


def test_per_market_cap_is_read_in_native_currency():
    """Лимит сделки Portals задаётся в TON, а не в Stars."""
    settings.portals_max_trade_ton = 3.0
    cap = settings.market_trade_cap(Market.PORTALS, Currency.TON)
    assert cap == Decimal("3.0")
    # Для Telegram лимит остаётся в Stars.
    settings.max_trade_stars = 5000
    assert settings.market_trade_cap(Market.TELEGRAM, Currency.STARS) == Decimal(5000)
