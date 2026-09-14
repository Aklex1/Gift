"""Торговые переключатели, общие для всех процессов.

Бот, воркер и панель — три отдельных процесса. Переключатель,
изменённый в одном, обязан подействовать в остальных: иначе
аварийный стоп из панели не остановит торгующий воркер.

Поэтому значения читаются из общего хранилища, а файл .env остаётся
запасным вариантом для первичной настройки.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from app.config import settings
from app.enums import Currency, Market, TradeMode
from app.services import store

log = logging.getLogger(__name__)

#: Площадки, для которых боевой режим вообще предусмотрен.
TRADABLE: tuple[Market, ...] = (Market.TELEGRAM, Market.PORTALS, Market.MRKT)

#: Валюта, в которой задаётся лимит сделки на площадке.
CAP_CURRENCY: dict[Market, Currency] = {
    Market.TELEGRAM: Currency.STARS,
    Market.PORTALS: Currency.TON,
    Market.MRKT: Currency.TON,
}

KEY_MODE = "TRADE_MODE"
KEY_KILL = "KILL_SWITCH"
KEY_EXPERIMENTAL_AUTO = "ALLOW_EXPERIMENTAL_AUTO"


def _write_key(market: Market) -> str:
    """Имя настройки боевого режима площадки."""
    return f"{market.value.upper()}_ENABLE_WRITE"


def _cap_key(market: Market) -> str:
    """Имя настройки лимита сделки площадки."""
    unit = "STARS" if CAP_CURRENCY[market] is Currency.STARS else "TON"
    return f"{market.value.upper()}_MAX_TRADE_{unit}"


def _as_bool(value: str | None, default: bool) -> bool:
    """Разобрать булево значение из хранилища."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _as_decimal(value: str | None, default: Decimal) -> Decimal:
    """Разобрать число из хранилища."""
    if value is None:
        return default
    try:
        return Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return default


# ----------------------------------------------------------------------
# Чтение
# ----------------------------------------------------------------------
def mode() -> TradeMode:
    """Текущий режим торговли."""
    raw = store.get(KEY_MODE)
    if raw:
        try:
            return TradeMode(raw.strip().lower())
        except ValueError:
            log.warning("Неизвестный режим %r в хранилище, беру из .env", raw)
    return settings.default_mode


def kill_switch() -> bool:
    """Включён ли аварийный стоп.

    Читается из общего хранилища, поэтому стоп, нажатый в панели или
    в боте, немедленно виден воркеру.
    """
    return _as_bool(store.get(KEY_KILL), settings.kill_switch)


def allow_experimental_auto() -> bool:
    """Допущены ли площадки без SLA в автономный режим."""
    return _as_bool(
        store.get(KEY_EXPERIMENTAL_AUTO), settings.allow_experimental_auto
    )


def write_enabled(market: Market | str) -> bool:
    """Разрешены ли боевые операции на площадке."""
    market = Market(market) if not isinstance(market, Market) else market
    if market not in TRADABLE:
        return False

    fallback = {
        Market.TELEGRAM: settings.telegram_enable_write,
        Market.PORTALS: settings.portals_enable_write,
        Market.MRKT: settings.mrkt_enable_write,
    }[market]
    return _as_bool(store.get(_write_key(market)), fallback)


def trade_cap(market: Market | str) -> Decimal | None:
    """Потолок одной сделки в валюте площадки. None = не задан."""
    market = Market(market) if not isinstance(market, Market) else market
    if market not in TRADABLE:
        return None

    fallback = {
        Market.TELEGRAM: Decimal(settings.max_trade_stars or 0),
        Market.PORTALS: Decimal(str(settings.portals_max_trade_ton or 0)),
        Market.MRKT: Decimal(str(settings.mrkt_max_trade_ton or 0)),
    }[market]
    cap = _as_decimal(store.get(_cap_key(market)), fallback)
    return cap if cap > 0 else None


def auto_markets() -> set[str]:
    """Площадки, которым разрешён автономный режим.

    Автономная торговля возможна только там, где включён боевой режим:
    отдельный белый список для этого больше не нужен.
    """
    return {m.value for m in TRADABLE if write_enabled(m)}


def cap_currency(market: Market) -> Currency:
    """В какой валюте задан лимит сделки площадки."""
    return CAP_CURRENCY.get(market, Currency.STARS)


# ----------------------------------------------------------------------
# Запись
# ----------------------------------------------------------------------
def set_mode(value: TradeMode, *, actor: str = "web") -> None:
    """Сменить режим торговли."""
    store.set(KEY_MODE, value.value)
    _audit(actor, "mode", value.value)


def set_kill_switch(value: bool, *, actor: str = "web") -> None:
    """Включить или снять аварийный стоп."""
    store.set(KEY_KILL, "true" if value else "false")
    _audit(actor, "kill_switch", value)


def set_allow_experimental_auto(value: bool, *, actor: str = "web") -> None:
    """Разрешить или запретить автономный режим для площадок без SLA."""
    store.set(KEY_EXPERIMENTAL_AUTO, "true" if value else "false")
    _audit(actor, "allow_experimental_auto", value)


def set_write_enabled(market: Market, value: bool, *, actor: str = "web") -> None:
    """Включить или выключить боевой режим площадки."""
    store.set(_write_key(market), "true" if value else "false")
    _audit(actor, f"write_enabled:{market.value}", value)


def set_trade_cap(market: Market, value: Decimal, *, actor: str = "web") -> None:
    """Задать потолок одной сделки на площадке."""
    store.set(_cap_key(market), format(value.normalize(), "f") if value > 0 else "")
    _audit(actor, f"trade_cap:{market.value}", str(value))


def _audit(actor: str, what: str, value: object) -> None:
    """Записать изменение предохранителя в журнал."""
    from app.db import session_scope
    from app.models import AuditLog

    try:
        with session_scope() as session:
            session.add(
                AuditLog(
                    actor=actor,
                    action=f"runtime.{what}",
                    target=str(value),
                    payload={"value": str(value)},
                )
            )
    except Exception as exc:  # noqa: BLE001 - аудит не должен ломать настройку
        log.warning("Не удалось записать в аудит %s=%s: %s", what, value, exc)
    log.warning("Изменён предохранитель %s = %s (кто: %s)", what, value, actor)


def snapshot() -> dict:
    """Срез всех переключателей для интерфейса."""
    return {
        "mode": mode(),
        "kill_switch": kill_switch(),
        "allow_experimental_auto": allow_experimental_auto(),
        "markets": {
            m.value: {
                "market": m,
                "write_enabled": write_enabled(m),
                "trade_cap": trade_cap(m),
                "currency": CAP_CURRENCY[m],
            }
            for m in TRADABLE
        },
    }
