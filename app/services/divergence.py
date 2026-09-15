"""Поиск расхождений цен между площадками.

Одна и та же модель стоит на площадках по-разному, и разница доходит
до десятков процентов: Light Sword / Enforcer в один момент стоил 7.20
на Portals, 7.42 на MRKT, 7.71 на Tonnel.

Это механика, которой не нужен прогноз: обе цены названы рынком.
Ошибиться можно только в комиссиях и в переносе подарка — и то и
другое считает ``arbitrage``, который здесь и вызывается.

Чем это отличается от обычного прохода. Сканер обходит площадки,
разрешённые стратегией, и ищет лот дешевле его собственной оценки.
Здесь наоборот: площадки берутся **все**, до которых бот дотягивается,
а оценка не нужна вовсе — сравниваются два живых предложения.

Поэтому связка находится и там, где обычный проход не найдёт ничего:
лот может стоять ровно по floor своей площадки — и всё равно быть
дешевле, чем тот же подарок у соседей.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import Capability, ListingDTO, SearchSkipped
from app.adapters.registry import get_adapter, tradable_markets
from app.db import session_scope
from app.enums import Market
from app.models import Candidate, Gift, utcnow
from app.services import arbitrage, salestats, store
from app.services import strategy as strategy_service

log = logging.getLogger(__name__)

#: Сколько лотов берём с площадки по коллекции. Связка живёт в самых
#: дешёвых: дорогой конец списка ни купить выгодно, ни сравнить.
PAGE = 50

#: Сколько коллекций обходим за проход. Каждая стоит запроса на каждой
#: площадке, поэтому список держим коротким.
COLLECTIONS = 8

#: Где лежит отчёт о последнем обходе.
KEY_REPORT = "DIVERGENCE_REPORT"

#: За какой срок коллекция считается встреченной.
RECENT_DAYS = 3

#: С какой доходности связка достойна отдельного уведомления.
NOTIFY_FROM = Decimal("0.25")


def watched_collections(session: Session, limit: int) -> list[str]:
    """Какие коллекции осматривать, в порядке доверия к источнику.

    1. Названные в стратегии — это прямое указание владельца.
    2. С широким разбросом цен сделок: там, где модели различаются в
       разы, расхождение между площадками встречается чаще.
    3. Коллекции недавних кандидатов — рынок уже что-то там показал.
    """
    wanted: list[str] = []

    def want(name: str | None) -> None:
        if name and name not in wanted and len(wanted) < limit:
            wanted.append(name)

    for strategy in strategy_service.active_strategies(session):
        for name in strategy.collections or []:
            want(name)

    for row in salestats.hunting_grounds(session, limit=limit * 2):
        want(row["collection"])

    since = utcnow() - dt.timedelta(days=RECENT_DAYS)
    rows = (
        session.query(Gift.collection)
        .join(Candidate, Candidate.gift_id == Gift.id)
        .filter(Candidate.created_at >= since)
        .distinct()
        .all()
    )
    for (name,) in rows:
        want(name)

    return wanted


def searchable_markets() -> list[Market]:
    """Площадки, где можно и искать, и торговать.

    Площадка только для чтения сюда не идёт: увидеть связку на ней
    можно, а исполнить — нет, и показывать такую находку значит звать
    к сделке, которой не будет.
    """
    return [
        market
        for market in tradable_markets()
        if get_adapter(market).supports(Capability.SEARCH)
    ]


async def collect(
    collections: list[str], markets: list[Market]
) -> tuple[list[ListingDTO], list[str]]:
    """Собрать лоты по коллекциям со всех площадок.

    Молчание одной площадки не отменяет обход: связка всё равно
    считается по тем, что ответили, — просто их меньше.
    """
    listings: list[ListingDTO] = []
    notes: list[str] = []

    for collection in collections:
        for market in markets:
            adapter = get_adapter(market)
            try:
                rows = await adapter.search(collection=collection, limit=PAGE)
            except SearchSkipped as exc:
                notes.append(f"{market.value}/{collection}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - одна площадка не рушит обход
                notes.append(f"{market.value}/{collection}: {type(exc).__name__}")
                continue
            listings.extend(rows)
    return listings, notes


async def sweep() -> dict:
    """Один обход в поисках расхождений.

    Returns:
        Найденные связки и чем обход обошёлся.
    """
    started = time.monotonic()
    if not arbitrage.enabled():
        return {"ok": False, "detail": "поиск расхождений выключен"}

    markets = searchable_markets()
    if len(markets) < 2:
        names = ", ".join(m.value for m in markets) or "ни одной"
        return {
            "ok": False,
            "detail": (
                f"сравнивать не с чем: площадок с доступным поиском — {names}. "
                f"Подключите ещё одну (панель → «Настройки» → токены)."
            ),
        }

    with session_scope() as session:
        collections = watched_collections(session, COLLECTIONS)
    if not collections:
        return {"ok": False, "detail": "нет коллекций для обхода"}

    listings, notes = await collect(collections, markets)
    with session_scope() as session:
        spreads = arbitrage.find(session, listings)

    report = {
        "ok": True,
        "at": utcnow().isoformat(timespec="seconds"),
        "markets": [m.value for m in markets],
        "collections": collections,
        "listings": len(listings),
        "spreads": [s.as_dict() for s in spreads[:20]],
        "found": len(spreads),
        "duration_sec": round(time.monotonic() - started, 1),
    }
    if notes:
        report["notes"] = notes[:8]
    store.set(KEY_REPORT, json.dumps(report, ensure_ascii=False))

    if spreads:
        log.info(
            "Расхождения: %s связок по %s лотам, лучшая %.1f%%",
            len(spreads), len(listings), float(spreads[0].net_roi) * 100,
        )
    return report


def worth_telling(spreads: list[dict]) -> list[dict]:
    """Связки, о которых стоит написать владельцу.

    Уведомление о каждой находке превращается в шум, который перестают
    читать, — а вместе с ним перестают читать и важное.
    """
    out = []
    for row in spreads:
        try:
            roi = Decimal(str(row.get("net_roi", "0")).rstrip("%")) / Decimal(100)
        except Exception:  # noqa: BLE001
            continue
        if roi >= NOTIFY_FROM:
            out.append(row)
    return out


def last_report() -> dict | None:
    """Отчёт о последнем обходе — для панели."""
    raw = store.get(KEY_REPORT)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
