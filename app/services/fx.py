"""Курсы валют из реальных источников.

ТЗ (раздел 10) требует timestamped FX snapshot с источником и спредом.
Раньше курс Stars/TON подставлялся константой — любое сравнение цен
между Telegram и внешними площадками было смещено на величину ошибки.

Источники:
    TON -> USD/RUB   TonAPI, /v2/rates
    Stars -> USD     официальные пакеты пополнения Telegram
                     (payments.getStarsTopupOptions)

Курс Stars/TON выводится из этих двух: сколько Stars можно купить на
сумму, равную одному TON.

Про спред: цена покупки Stars выше их ценности при продаже — Telegram
зарабатывает на разнице. Поэтому к расчётному курсу применяется
консервативная поправка: при пересчёте цены в Stars мы считаем, что
Stars обходятся дороже, а не дешевле.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import httpx

from app.config import settings
from app.db import session_scope
from app.enums import Currency
from app.models import FxSnapshot, utcnow
from app.services import marketdata, store

log = logging.getLogger(__name__)

#: Сколько ждать ответа источника.
TIMEOUT = 15.0

#: Консервативная поправка на спред: 3%.
#: Курс занижается, чтобы сделка не выглядела выгоднее, чем есть.
DEFAULT_SPREAD = Decimal("0.03")

#: Ключ, под которым хранится отчёт о последнем обновлении.
LAST_FX_KEY = "LAST_FX_REPORT"

#: Ручная цена звезды в долларах — запасной вариант, когда сессия
#: Telegram недоступна и официальные пакеты не прочитать.
MANUAL_STAR_USD_KEY = "FX_STAR_USD"


async def fetch_ton_rates() -> dict[str, Decimal]:
    """Курс TON к доллару и рублю из TonAPI."""
    url = f"{settings.tonapi_base_url.rstrip('/')}/v2/rates"
    headers = {"Accept": "application/json"}
    key = settings.tonapi_key
    if key:
        headers["Authorization"] = f"Bearer {key}"

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.get(
            url, params={"tokens": "ton", "currencies": "usd,rub"}, headers=headers
        )
        response.raise_for_status()
        data = response.json()

    prices = ((data.get("rates") or {}).get("TON") or {}).get("prices") or {}
    out: dict[str, Decimal] = {}
    for code in ("USD", "RUB"):
        value = prices.get(code)
        if value:
            out[code] = Decimal(str(value))
    if not out:
        raise ValueError("TonAPI не вернул цен TON")
    return out


async def fetch_stars_price_usd() -> Decimal:
    """Сколько долларов стоит одна звезда.

    Берётся из официальных пакетов пополнения: это цена, по которой
    Telegram продаёт Stars, то есть реальная стоимость приобретения.
    Выбирается самый выгодный пакет — по нему и считаем.
    """
    from telethon.tl import functions

    from app.adapters.registry import get_adapter
    from app.enums import Market

    adapter = get_adapter(Market.TELEGRAM)
    result = await adapter.gateway.call(
        functions.payments.GetStarsTopupOptionsRequest()
    )

    best: Decimal | None = None
    for option in result or []:
        stars = getattr(option, "stars", 0)
        amount = getattr(option, "amount", 0)
        currency = str(getattr(option, "currency", "") or "").upper()
        if currency != "USD" or not stars or not amount:
            continue
        # amount приходит в сотых долях валюты.
        per_star = Decimal(amount) / Decimal(100) / Decimal(stars)
        if best is None or per_star < best:
            best = per_star

    if best is None or best <= 0:
        raise ValueError("Telegram не вернул пакетов Stars в долларах")
    return best


def manual_star_usd() -> Decimal | None:
    """Заданная вручную цена звезды в долларах."""
    raw = store.get(MANUAL_STAR_USD_KEY)
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except Exception:  # noqa: BLE001
        return None
    return value if value > 0 else None


def set_manual_star_usd(value: Decimal | None, *, actor: str = "web") -> None:
    """Задать или очистить ручную цену звезды."""
    from app.services import runtime

    store.set(
        MANUAL_STAR_USD_KEY,
        format(value.normalize(), "f") if value and value > 0 else "",
    )
    runtime._audit(actor, "fx.star_usd", str(value or "очищено"))


def set_spread(value: Decimal, *, actor: str = "web") -> None:
    """Задать поправку на спред."""
    from app.services import runtime

    store.set("FX_SPREAD", format(value.normalize(), "f"))
    runtime._audit(actor, "fx.spread", str(value))


def spread() -> Decimal:
    """Текущая поправка на спред."""
    raw = store.get("FX_SPREAD")
    if raw:
        try:
            value = Decimal(raw)
            if 0 <= value < 1:
                return value
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_SPREAD


async def refresh() -> dict:
    """Обновить курсы и записать снапшоты.

    Ошибка одного источника не мешает сохранить то, что получилось:
    курс TON к рублю полезен сам по себе, даже если цена Stars
    недоступна.
    """
    import json

    report: dict[str, object] = {"at": utcnow().isoformat(timespec="seconds")}

    ton_rates: dict[str, Decimal] = {}
    try:
        ton_rates = await asyncio.wait_for(fetch_ton_rates(), timeout=TIMEOUT)
        report["ton_usd"] = str(ton_rates.get("USD", ""))
        report["ton_rub"] = str(ton_rates.get("RUB", ""))
    except Exception as exc:  # noqa: BLE001 - источник может быть недоступен
        report["ton_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("Курс TON не получен: %s", exc)

    star_usd: Decimal | None = None
    star_source = "telegram_topup"
    try:
        star_usd = await asyncio.wait_for(fetch_stars_price_usd(), timeout=TIMEOUT)
        report["star_usd"] = str(star_usd)
    except Exception as exc:  # noqa: BLE001 - нужна авторизованная сессия
        report["star_error"] = f"{type(exc).__name__}: {exc}"
        log.info("Цена Stars из Telegram не получена: %s", exc)
        # Запасной вариант: значение, заданное владельцем вручную.
        star_usd = manual_star_usd()
        if star_usd:
            star_source = "вручную"
            report["star_usd"] = str(star_usd)
            report["star_note"] = "использовано значение, заданное вручную"

    with session_scope() as session:
        if ton_rates.get("USD"):
            marketdata.record_fx(
                session, Currency.TON, Currency.USD, ton_rates["USD"], source="tonapi"
            )
        if ton_rates.get("RUB"):
            marketdata.record_fx(
                session, Currency.TON, Currency.RUB, ton_rates["RUB"], source="tonapi"
            )

        if star_usd and star_usd > 0:
            # Сколько долларов стоит одна звезда — записываем как курс.
            marketdata.record_fx(
                session,
                Currency.STARS,
                Currency.USD,
                star_usd,
                source=star_source,
            )

            if ton_rates.get("USD"):
                # Сколько Stars приходится на один TON, с поправкой на спред.
                raw_rate = ton_rates["USD"] / star_usd
                adjusted = raw_rate * (Decimal(1) - spread())
                marketdata.record_fx(
                    session,
                    Currency.TON,
                    Currency.STARS,
                    adjusted,
                    source=f"tonapi + {star_source}, спред {spread():.0%}",
                )
                report["ton_stars_raw"] = str(raw_rate.quantize(Decimal("0.01")))
                report["ton_stars"] = str(adjusted.quantize(Decimal("0.01")))

    store.set(LAST_FX_KEY, json.dumps(report, ensure_ascii=False))
    log.info("Курсы обновлены: %s", report)
    return report


def last_report() -> dict | None:
    """Отчёт о последнем обновлении курсов."""
    import json

    raw = store.get(LAST_FX_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def snapshot(session) -> dict:
    """Текущие курсы для интерфейса."""
    # Названия пар — для человека, поэтому TON показывается как GRAM.
    pairs = [
        (Currency.TON, Currency.STARS, "GRAM → Stars"),
        (Currency.TON, Currency.USD, "GRAM → USD"),
        (Currency.TON, Currency.RUB, "GRAM → RUB"),
        (Currency.STARS, Currency.USD, "Stars → USD"),
    ]
    out = []
    for base, quote, title in pairs:
        row = (
            session.query(FxSnapshot)
            .filter_by(base=base, quote=quote)
            .order_by(FxSnapshot.taken_at.desc())
            .first()
        )
        out.append(
            {
                "title": title,
                "rate": Decimal(row.rate) if row else None,
                "source": row.source if row else None,
                "at": row.taken_at if row else None,
                # Значение по умолчанию — признак того, что источник
                # недоступен и расчёты приблизительны.
                "is_default": bool(row and row.source == "default"),
            }
        )
    return {"pairs": out, "spread": spread(), "report": last_report()}
