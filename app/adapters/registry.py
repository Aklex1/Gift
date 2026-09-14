"""Реестр адаптеров площадок.

Единая точка получения адаптера и синхронизации матрицы возможностей
в БД — то, что UI показывает как «статус коннекторов».
"""

from __future__ import annotations

import logging

from app.adapters.base import Capability, CapabilityStatus, MarketAdapter
from app.adapters.getgems import GetgemsAdapter
from app.adapters.mrkt import MrktAdapter
from app.adapters.portals import PortalsAdapter
from app.adapters.telegram_mtproto import TelegramAdapter
from app.adapters.tonnel import TonnelAdapter
from app.db import session_scope
from app.enums import Market
from app.models import AdapterCapability, utcnow

log = logging.getLogger(__name__)

_ADAPTERS: dict[Market, MarketAdapter] = {}


def get_adapter(market: Market | str) -> MarketAdapter:
    """Получить (и закэшировать) адаптер площадки."""
    market = Market(market) if not isinstance(market, Market) else market
    if market not in _ADAPTERS:
        builders = {
            Market.TELEGRAM: TelegramAdapter,
            Market.PORTALS: PortalsAdapter,
            Market.MRKT: MrktAdapter,
            Market.TONNEL: TonnelAdapter,
            Market.GETGEMS: GetgemsAdapter,
        }
        _ADAPTERS[market] = builders[market]()
    return _ADAPTERS[market]


def telegram_adapters(session) -> list[TelegramAdapter]:
    """Адаптеры Telegram по одному на каждый пригодный аккаунт.

    Поиск распределяется между ними: FloodWait считается по аккаунту,
    поэтому несколько сессий позволяют сканировать чаще.
    """
    from app.adapters.telegram_gateway import gateway_for
    from app.services import accounts as accounts_service

    out: list[TelegramAdapter] = []
    for account in accounts_service.usable(session):
        out.append(TelegramAdapter(gateway_for(account)))
    return out


def telegram_adapter_for(account) -> TelegramAdapter:
    """Адаптер Telegram конкретного аккаунта."""
    from app.adapters.telegram_gateway import gateway_for

    return TelegramAdapter(gateway_for(account))


def all_adapters() -> list[MarketAdapter]:
    """Все адаптеры в фиксированном порядке."""
    return [get_adapter(m) for m in Market]


def capability_matrix() -> dict[str, dict[str, str]]:
    """Матрица возможностей для UI: рынок -> возможность -> статус."""
    matrix: dict[str, dict[str, str]] = {}
    for adapter in all_adapters():
        matrix[adapter.market.value] = {
            cap.value: adapter.status_of(cap).value for cap in Capability
        }
    return matrix


def sync_capabilities() -> None:
    """Записать текущую матрицу возможностей в БД."""
    with session_scope() as session:
        for adapter in all_adapters():
            for cap in Capability:
                status = adapter.status_of(cap)
                row = (
                    session.query(AdapterCapability)
                    .filter_by(market=adapter.market, capability=cap)
                    .one_or_none()
                )
                if row is None:
                    row = AdapterCapability(market=adapter.market, capability=cap)
                    session.add(row)
                row.status = status
    log.info("Матрица возможностей синхронизирована")


async def probe_all() -> dict[str, dict[str, str]]:
    """Живая проверка read-операций всех площадок.

    Write-операции не пробуются — это тратило бы реальные деньги.
    """
    report: dict[str, dict[str, str]] = {}
    for adapter in all_adapters():
        market_report: dict[str, str] = {}
        try:
            results = await adapter.probe()
        except Exception as exc:  # noqa: BLE001 - probe не должен падать
            report[adapter.market.value] = {"_error": f"{type(exc).__name__}: {exc}"}
            continue

        with session_scope() as session:
            for cap, (ok, detail) in results.items():
                market_report[cap.value] = ("OK: " if ok else "FAIL: ") + detail
                row = (
                    session.query(AdapterCapability)
                    .filter_by(market=adapter.market, capability=cap)
                    .one_or_none()
                )
                if row is None:
                    row = AdapterCapability(
                        market=adapter.market,
                        capability=cap,
                        status=adapter.status_of(cap),
                    )
                    session.add(row)
                row.last_probe_at = utcnow()
                row.last_probe_ok = ok
                row.detail = detail[:500]
                if not ok and row.status is CapabilityStatus.EXPERIMENTAL:
                    # Приватный API отвалился — торговать по нему нельзя.
                    row.status = CapabilityStatus.UNAVAILABLE
        report[adapter.market.value] = market_report
    return report


async def close_all() -> None:
    """Закрыть все сетевые соединения адаптеров и сессии аккаунтов."""
    from app.adapters import telegram_gateway

    for adapter in list(_ADAPTERS.values()):
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            pass
    _ADAPTERS.clear()
    await telegram_gateway.close_all()
