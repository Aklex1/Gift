"""Быстрый контур: частый обход горячих коллекций.

Зачем. Недооценённый лот живёт секунды, а полный проход занимает
минуты и обходит весь рынок. Догнать чужую ошибку в цене такой обход
не может в принципе — не потому, что медленно считает, а потому, что
между двумя взглядами на коллекцию проходит слишком много времени.

Что делает этот контур. Смотрит только первую страницу — самые дешёвые
лоты — и только по десятку коллекций, где дешёвые входы вообще
случаются. Один запрос на коллекцию, ничего больше.

Чего он не делает. Он не решает, покупать ли. Он только замечает, что
лот заметно дешевле известного, и передаёт его в тот же самый полный
расчёт, что и обычный проход, — со всеми проверками: расхождение
источников, потолок площадки, свежесть курса. Иначе быстрый контур
был бы просто быстрым способом купить мусор.

Горячий список берётся из разброса цен: коллекция, где все сделки в
узкой полосе, дешёвых входов не даёт, сколько её ни карауль.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections import Counter
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import Capability, ListingDTO, SearchSkipped
from app.adapters.registry import get_adapter
from app.db import session_scope
from app.enums import Market
from app.models import Candidate, Gift, utcnow
from app.services import gifts as gifts_service
from app.services import marketdata, runtime, salestats, scanner
from app.services import strategy as strategy_service
from app.services import store

log = logging.getLogger(__name__)

#: Сколько самых дешёвых лотов смотрим в коллекции. Больше незачем:
#: находка — это всегда верх списка, отсортированного по цене.
PAGE = 20

#: Где лежит отчёт о последнем обходе.
KEY_REPORT = "FAST_LANE_REPORT"

#: За какой срок считаем коллекцию «встреченной» при подборе горячего
#: списка по кандидатам.
RECENT = 3


def hot_pairs(session: Session, limit: int) -> list[tuple[Market, str]]:
    """Что обходить часто: площадка и коллекция.

    Два источника, в порядке доверия:

    1. Разброс цен по состоявшимся сделкам. Там, где медиана заметно
       выше дешёвого хвоста, дешёвые входы случаются регулярно.
    2. Коллекции недавних кандидатов — рынок уже показал, что здесь
       что-то находится.

    Площадки берутся те, где стратегии разрешили искать: обходить
    коллекцию там, где по ней не торгуют, — трата запросов.
    """
    markets: list[Market] = []
    collections: list[str] = []
    for strategy in strategy_service.active_strategies(session):
        for raw in strategy.markets or []:
            try:
                market = Market(str(raw).lower())
            except ValueError:
                continue
            if market not in markets:
                markets.append(market)
        collections.extend(strategy.collections or [])
    if not markets:
        return []

    wanted: list[str] = []

    def want(name: str | None) -> None:
        if name and name not in wanted:
            wanted.append(name)

    # Стратегия могла назвать коллекции прямо — это самое сильное
    # указание, и спорить с ним незачем.
    for name in collections:
        want(name)

    for row in salestats.hunting_grounds(session, limit=limit * 2):
        want(row["collection"])

    since = utcnow() - dt.timedelta(days=RECENT)
    rows = (
        session.query(Gift.collection)
        .join(Candidate, Candidate.gift_id == Gift.id)
        .filter(Candidate.created_at >= since)
        .distinct()
        .all()
    )
    for (name,) in rows:
        want(name)

    pairs: list[tuple[Market, str]] = []
    for name in wanted:
        for market in markets:
            adapter = get_adapter(market)
            if not adapter.supports(Capability.SEARCH):
                continue
            pairs.append((market, name))
            if len(pairs) >= limit:
                return pairs
    return pairs


def looks_cheap(
    session: Session, dto: ListingDTO, price_stars: Decimal, min_gap: Decimal
) -> bool:
    """Заметно ли лот дешевле всего, что о таких подарках известно.

    Это скрининг, а не решение. Потолок берётся без запаса и по уже
    накопленным данным, без единого запроса, — иначе частый обход
    превратился бы в такой же медленный, как обычный проход.
    """
    ceiling = scanner.cheap_ceiling(session, dto, margin=Decimal(1))
    if ceiling is None or ceiling <= 0:
        # О подарке ничего не известно. Пропускать такие в полный
        # расчёт — значит тратить на них то самое время; но и
        # отбрасывать нельзя. Отдаём: их единицы.
        return True
    return price_stars <= ceiling * (Decimal(1) - min_gap)


async def sweep() -> dict:
    """Один обход горячего списка.

    Returns:
        Что увидели и сколько из этого дошло до полного расчёта.
    """
    started = time.monotonic()
    if not runtime.fast_lane_enabled():
        return {"ok": False, "detail": "быстрый контур выключен"}
    if runtime.kill_switch():
        return {"ok": False, "detail": "kill switch включён"}

    min_gap = runtime.fast_gap()
    with session_scope() as session:
        strategies = strategy_service.active_strategies(session)
        if not strategies:
            return {"ok": False, "detail": "нет включённых стратегий"}
        plan = scanner.active_plan(strategies)
        pairs = hot_pairs(session, runtime.fast_pairs())
    if not pairs:
        return {"ok": False, "detail": "горячий список пуст"}

    seen = 0
    screened: list[ListingDTO] = []
    notes: list[str] = []

    for market, collection in pairs:
        adapter = get_adapter(market)
        try:
            rows = await adapter.search(collection=collection, limit=PAGE)
        except SearchSkipped as exc:
            notes.append(f"{market.value}/{collection}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - один промах не рушит обход
            notes.append(f"{market.value}/{collection}: {type(exc).__name__}")
            continue

        seen += len(rows)
        with session_scope() as session:
            for dto in rows:
                price_stars = marketdata.to_stars(session, dto.price, dto.currency)
                if price_stars is None or price_stars <= 0:
                    continue
                if looks_cheap(session, dto, price_stars, min_gap):
                    screened.append(dto)

    # Прошедшие скрининг идут обычным путём — со всеми проверками.
    created = 0
    rejections: Counter[str] = Counter()
    if screened:
        with session_scope() as session:
            for dto in screened:
                gifts_service.upsert_listing(session, dto)
        for dto in screened:
            try:
                made, reasons = await scanner.evaluate_listing(dto, plan)
                created += made
                rejections.update(reasons)
            except Exception as exc:  # noqa: BLE001
                log.exception("Быстрый контур: ошибка оценки %s: %s",
                              dto.external_id, exc)

    report = {
        "ok": True,
        "at": utcnow().isoformat(timespec="seconds"),
        "pairs": [f"{m.value}/{c}" for m, c in pairs],
        "seen": seen,
        "screened": len(screened),
        "candidates": created,
        "duration_sec": round(time.monotonic() - started, 1),
        "min_gap": str(min_gap),
    }
    if notes:
        report["notes"] = notes[:5]
    store.set(KEY_REPORT, json.dumps(report, ensure_ascii=False))

    if created:
        log.info(
            "Быстрый контур: %s лотов, %s в проверку, %s кандидатов за %s c",
            seen, len(screened), created, report["duration_sec"],
        )
    return report


def last_report() -> dict | None:
    """Отчёт о последнем обходе — для панели."""
    raw = store.get(KEY_REPORT)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
