"""Балансы кошельков на торговых площадках.

Это не то же самое, что баланс Telegram-аккаунта: у Portals и MRKT
внутри площадки свой кошелёк, и покупка идёт именно с него. Знать
его остаток нужно, чтобы понимать, есть ли вообще на что торговать.

Значения опрашиваются воркером и кладутся в общее хранилище: панель
показывает их мгновенно, не дожидаясь сети.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from decimal import Decimal

from app.adapters.base import Capability
from app.adapters.registry import get_adapter
from app.enums import Market, display_currency
from app.services import runtime, store

log = logging.getLogger(__name__)

#: Сколько ждать ответа площадки.
TIMEOUT = 15.0

#: Площадки, у которых есть собственный кошелёк.
WITH_WALLET: tuple[Market, ...] = (Market.PORTALS, Market.MRKT, Market.TONNEL)


def _key(market: Market) -> str:
    """Ключ хранения баланса площадки."""
    return f"BALANCE_{market.value.upper()}"


async def refresh(markets: tuple[Market, ...] = WITH_WALLET) -> dict:
    """Опросить балансы площадок и сохранить результат."""
    report: dict[str, str] = {}

    for market in markets:
        adapter = get_adapter(market)
        if not adapter.supports(Capability.BALANCE):
            # Без токена площадка баланс не отдаёт. Причину надо
            # сохранить: иначе в панели будет пустая клетка, по которой
            # не понять, чего не хватает.
            store.set(
                _key(market),
                json.dumps(
                    {
                        "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
                        "error": (
                            "нужен токен площадки — задайте в панели, "
                            "раздел «Настройки»"
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
            report[market.value] = "нет токена"
            continue

        record: dict[str, object] = {
            "at": dt.datetime.utcnow().isoformat(timespec="seconds"),
            # Адрес в записи: по ошибке вида «хост не резолвится» иначе
            # не понять, что в .env остался прежний домен площадки.
            "url": getattr(adapter, "base_url", ""),
        }
        try:
            rows = await asyncio.wait_for(adapter.balance(), timeout=TIMEOUT)
            total = sum((Decimal(r.amount) for r in rows), Decimal(0))
            currency = rows[0].currency.value if rows else adapter.native_currency.value
            record.update(amount=str(total), currency=currency)
            report[market.value] = f"{total} {display_currency(currency)}"
        except asyncio.TimeoutError:
            record["error"] = f"площадка не ответила за {TIMEOUT:.0f} c"
            report[market.value] = record["error"]
        except Exception as exc:  # noqa: BLE001 - одна площадка не ломает опрос
            text = f"{type(exc).__name__}: {exc}"
            if "No address associated" in text or "getaddrinfo" in text:
                text = (
                    f"домен {record['url']} не резолвится — вероятно, в .env "
                    f"остался прежний адрес. Выполните: gift-cli env-sync"
                )
            record["error"] = text
            report[market.value] = text
            log.debug("Баланс %s недоступен: %s", market.value, exc)

        store.set(_key(market), json.dumps(record, ensure_ascii=False))

    log.info("Балансы площадок обновлены: %s", report)
    return report


def snapshot() -> list[dict]:
    """Балансы площадок для интерфейса.

    Показываются только площадки, с которыми есть смысл работать:
    включённые в боевой режим либо уже вернувшие баланс.
    """
    out: list[dict] = []
    for market in WITH_WALLET:
        raw = store.get(_key(market))
        enabled = runtime.write_enabled(market)
        if not raw and not enabled:
            continue

        record: dict = {}
        if raw:
            try:
                record = json.loads(raw)
            except (ValueError, TypeError):
                record = {}

        amount = record.get("amount")
        out.append(
            {
                "market": market.value,
                "title": {"portals": "Portals", "mrkt": "MRKT", "tonnel": "Tonnel"}.get(
                    market.value, market.value
                ),
                "amount": Decimal(amount) if amount is not None else None,
                "currency": record.get("currency", "TON"),
                "at": record.get("at"),
                "url": record.get("url"),
                "error": record.get("error"),
                "enabled": enabled,
            }
        )
    return out
