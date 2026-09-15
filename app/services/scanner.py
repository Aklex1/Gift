"""Сканер рынков: поиск возможностей заработка.

Проход сканера:
    1. Обновить курс Stars/TON (FX-снапшот с таймстемпом).
    2. Собрать активные лоты по всем площадкам стратегий.
    3. Сохранить историю продаж как market facts.
    4. Оценить каждый лот: ROI после комиссий, риск, качество данных.
    5. Сохранить прошедшие фильтр как кандидатов.

Сканер ничего не покупает. Решение об исполнении принимает executor
в соответствии с режимом SAFE/SEMI/AUTO.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import (
    AdapterError,
    AuthRequired,
    Capability,
    CapabilityStatus,
    ListingDTO,
    RateLimited,
    SearchSkipped,
)
from app.adapters.registry import get_adapter
from app.adapters.telegram_mtproto import TelegramAdapter
from app.db import session_scope
from app.enums import Currency, Market
from app.models import Candidate, utcnow
from app.services import gifts as gifts_service
from app.services import arbitrage
from app.services import marketdata, strategy as strategy_service, valuation
from app.services.marketdata import MarketSnapshot

log = logging.getLogger(__name__)

def to_decimal_or_none(value: object) -> Decimal | None:
    """Мягкое приведение к Decimal для необязательных полей."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


#: Сколько живёт кандидат, прежде чем считать цену устаревшей.
CANDIDATE_TTL = dt.timedelta(minutes=10)

#: Ключ, под которым хранится отчёт о последнем проходе.
LAST_SCAN_KEY = "LAST_SCAN_REPORT"

#: Человеческие названия причин отсева — для интерфейса.
REJECTION_LABELS = {
    "filters": "не подходит под фильтры стратегии",
    "price_range": "цена вне коридора стратегии",
    "positions": "достигнут лимит открытых позиций",
    "confidence": "мало рыночных данных для оценки",
    "blockers": "сделка убыточна после комиссий",
    "roi": "прибыль ниже порога стратегии",
    "risk": "риск выше допустимого",
}


def save_report(report: dict) -> None:
    """Сохранить отчёт о проходе, чтобы панель могла его показать."""
    import json

    from app.services import store

    try:
        store.set(LAST_SCAN_KEY, json.dumps(report, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001 - отчёт не важнее самого скана
        log.debug("Не удалось сохранить отчёт скана: %s", exc)


def save_failure(exc: BaseException) -> None:
    """Записать, что проход сорвался, вместе с причиной."""
    save_report(
        {
            "listings": 0,
            "facts": 0,
            "candidates": 0,
            "markets": {},
            "rejections": {},
            # Список стратегий намеренно не заполняется: при сорванном
            # проходе мы о них ничего не знаем, а пустой список
            # прочитался бы как «стратегий нет».
            "started_at": utcnow().isoformat(timespec="seconds"),
            "finished_at": utcnow().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "note": "проход сорвался с ошибкой — смотрите journalctl -u gift-worker",
        }
    )


def last_report() -> dict | None:
    """Отчёт о последнем проходе сканера."""
    import json

    from app.services import store

    raw = store.get(LAST_SCAN_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


async def refresh_fx(session: Session) -> Decimal | None:
    """Обновить курс TON->Stars.

    Курс выводится из сопоставимых цен: берём медианную цену подарков,
    выставленных и в Stars (Telegram), и в TON (внешние площадки).
    Пока сопоставимой пары нет, используется значение по умолчанию.
    """
    from app.models import Listing

    ton_prices = [
        Decimal(row.price)
        for row in session.query(Listing)
        .filter(Listing.is_active.is_(True), Listing.currency == Currency.TON)
        .limit(200)
        .all()
        if row.price
    ]
    if not ton_prices:
        return None

    # Прямой рыночный курс здесь получить неоткуда — фиксируем текущее
    # допущение снапшотом, чтобы расчёты были воспроизводимы.
    rate = marketdata.latest_fx(session, Currency.TON, Currency.STARS)
    if rate is None:
        # Курс обновляет отдельная задача из реальных источников;
        # подменять его здесь значило бы закрепить неверное значение.
        log.warning(
            "Курс TON->Stars ещё не получен. Расчёты приблизительны, "
            "пока не отработает обновление курсов."
        )
    return rate


def _search_adapters(market: Market) -> list:
    """Адаптеры, которыми можно вести поиск на площадке.

    Для Telegram это по одному адаптеру на каждый пригодный аккаунт:
    FloodWait считается по аккаунту, поэтому коллекции распределяются
    между ними и общий проход идёт быстрее, не приближаясь к порогу.
    """
    if market is not Market.TELEGRAM:
        return [get_adapter(market)]

    from app.adapters.registry import telegram_adapters

    with session_scope() as session:
        adapters = telegram_adapters(session)
    return adapters or [get_adapter(market)]


async def collect_listings(
    market: Market, *, collections: list[str], limit: int
) -> tuple[list[ListingDTO], list[str]]:
    """Собрать активные лоты площадки по списку коллекций.

    Returns:
        (лоты, причины). Причины нужны ровно для одного случая, который
        раньше выглядел одинаково при совершенно разных бедах: площадка
        вернула ноль лотов. Без них в панели оставалась догадка
        «проверьте токены и сеть», по которой ничего не найти.
    """
    notes: list[str] = []
    everything = _search_adapters(market)
    adapters = [a for a in everything if a.supports(Capability.SEARCH)]
    if not adapters:
        if not everything:
            notes.append("нет ни одного аккаунта для поиска")
        else:
            reason = {
                Market.TELEGRAM: "сессия не авторизована (gift-cli login)",
            }.get(market, "нет токена площадки — задайте в «Настройках»")
            notes.append(f"поиск недоступен: {reason}")
        return ([], notes)

    out: list[ListingDTO] = []
    targets: list[str | None] = list(collections) if collections else [None]
    #: Коллекции раздаются аккаунтам по кругу.
    exhausted: set[int] = set()
    empty: list[str] = []

    for index, collection in enumerate(targets):
        if len(exhausted) >= len(adapters):
            notes.append("все аккаунты выбыли, часть коллекций не осмотрена")
            break
        # Пропускаем аккаунты, которые уже упёрлись в лимит.
        for offset in range(len(adapters)):
            slot = (index + offset) % len(adapters)
            if slot not in exhausted:
                break
        else:
            break

        adapter = adapters[slot]
        label = getattr(adapter, "label", market.value)
        where = collection or "весь рынок"
        try:
            found = await adapter.search(collection=collection, limit=limit)
        except SearchSkipped as exc:
            # Беда одной коллекции, а не площадки: остальные смотрим.
            notes.append(str(exc))
            continue
        except RateLimited as exc:
            log.warning("%s (%s): лимит запросов, аккаунт пропущен: %s",
                        market.value, label, exc)
            notes.append(f"{label}: лимит запросов Telegram, аккаунт пропущен")
            exhausted.add(slot)
        except AuthRequired as exc:
            log.warning("%s (%s): нет доступа: %s", market.value, label, exc)
            notes.append(f"{label}: токен не принят ({exc})")
            exhausted.add(slot)
        except AdapterError as exc:
            log.warning("%s (%s): поиск недоступен: %s", market.value, label, exc)
            notes.append(f"{label}: {exc}")
            exhausted.add(slot)
        except Exception as exc:  # noqa: BLE001 - один аккаунт не роняет скан
            log.exception("%s (%s): ошибка поиска: %s", market.value, label, exc)
            notes.append(f"{label}: {type(exc).__name__}: {exc}")
            exhausted.add(slot)
        else:
            if found:
                out.extend(found)
            else:
                empty.append(where)

    if empty and not out:
        notes.append(
            "площадка ответила пусто по: " + ", ".join(empty[:5])
            + (" и др." if len(empty) > 5 else "")
        )
    return (out, notes)


async def collect_history(market: Market, *, collections: list[str]) -> int:
    """Загрузить историю продаж площадки в market facts."""
    adapter = get_adapter(market)
    if not adapter.supports(Capability.HISTORY):
        return 0

    saved = 0
    targets: list[str | None] = list(collections) if collections else [None]
    for collection in targets[:5]:
        try:
            sales = await adapter.history(collection=collection, limit=100)
        except (AdapterError, Exception) as exc:  # noqa: BLE001
            log.debug("%s: история недоступна: %s", market.value, exc)
            break
        with session_scope() as session:
            saved += marketdata.record_facts(session, sales, market)
    return saved


async def snapshot_for_listing(
    session: Session, dto: ListingDTO
) -> MarketSnapshot:
    """Построить срез рынка для конкретного лота.

    Для Telegram приоритет у официальной оценки
    ``payments.getUniqueStarGiftValueInfo``: floor, средняя цена и
    последняя продажа приходят от самой площадки.
    """
    if dto.market is Market.TELEGRAM and dto.external_id:
        adapter = get_adapter(Market.TELEGRAM)
        if isinstance(adapter, TelegramAdapter):
            try:
                info = await adapter.value_info(dto.external_id)
                if info.get("floor_price") or info.get("average_price"):
                    return marketdata.snapshot_from_telegram(
                        info, collection=dto.gift.collection, model=dto.gift.model
                    )
            except Exception as exc:  # noqa: BLE001 - откатываемся на свою выборку
                log.debug("value_info для %s недоступен: %s", dto.external_id, exc)

    if dto.market is Market.PORTALS and dto.gift.model:
        # Floor по модели — самая точная оценка, доступная сразу.
        adapter = get_adapter(Market.PORTALS)
        floors = getattr(adapter, "attribute_floors", None)
        if floors is not None:
            try:
                data = await floors(dto.gift.collection)
                model_floor = (data.get("models") or {}).get(dto.gift.model)
                if model_floor:
                    # Portals отдаёт floor в TON, а вся оценка сделки
                    # идёт в Stars. Без приведения справедливая цена
                    # оказывалась в 65 раз ниже цены покупки, и любой
                    # лот выглядел безнадёжно переоценённым.
                    floor_stars = marketdata.to_stars(
                        session, Decimal(str(model_floor)), Currency.TON
                    )
                    collection_floor = to_decimal_or_none(
                        (dto.raw or {}).get("floor_price")
                    )
                    if collection_floor is not None:
                        collection_floor = marketdata.to_stars(
                            session, collection_floor, Currency.TON
                        )
                    if floor_stars:
                        return marketdata.snapshot_from_attribute_floor(
                            collection=dto.gift.collection,
                            model=dto.gift.model,
                            model_floor=floor_stars,
                            collection_floor=collection_floor,
                            listed_count=len(data.get("models") or {}),
                        )
            except Exception as exc:  # noqa: BLE001 - откат на историю
                log.debug("Portals: floor модели недоступен: %s", exc)

    return marketdata.snapshot_for(
        session, collection=dto.gift.collection, model=dto.gift.model
    )


async def scan_once() -> dict:
    """Один полный проход сканера.

    Returns:
        Сводка: сколько лотов просмотрено и сколько кандидатов создано.
    """
    started = utcnow()
    # Отмечаем, что проход пошёл. Без этого долгий скан выглядит в
    # панели точно так же, как остановленный воркер, и человек идёт
    # искать сбой, которого нет.
    save_report(
        {
            "running": True,
            "started_at": started.isoformat(timespec="seconds"),
        }
    )
    report: dict = {
        "listings": 0,
        "facts": 0,
        "candidates": 0,
        "markets": {},
        "rejections": {},
        "strategies": [],
        "started_at": started.isoformat(timespec="seconds"),
    }

    with session_scope() as session:
        strategies = strategy_service.active_strategies(session)
        report["strategies"] = [s.name for s in strategies]
        if not strategies:
            report["note"] = (
                "нет включённых стратегий — включите на странице «Стратегии» в боте"
            )
            report["finished_at"] = utcnow().isoformat(timespec="seconds")
            save_report(report)
            log.info("Нет включённых стратегий — сканирование пропущено")
            return report
        # Материализуем нужные поля: сессия закроется до асинхронных вызовов.
        plan = [
            {
                "id": s.id,
                "name": s.name,
                "markets": [str(m).lower() for m in (s.markets or [])],
                "collections": list(s.collections or []),
            }
            for s in strategies
        ]

    wanted_markets: dict[Market, set[str]] = {}
    for item in plan:
        for market_name in item["markets"] or [Market.TELEGRAM.value]:
            try:
                market = Market(market_name)
            except ValueError:
                continue
            wanted_markets.setdefault(market, set()).update(item["collections"])

    # --- сбор данных ---
    all_listings: list[ListingDTO] = []
    for market, collections in wanted_markets.items():
        rows, notes = await collect_listings(
            market, collections=sorted(collections), limit=200
        )
        report["markets"][market.value] = len(rows)
        if notes:
            report.setdefault("market_notes", {})[market.value] = notes
        all_listings.extend(rows)

        facts = await collect_history(market, collections=sorted(collections))
        report["facts"] += facts

    report["listings"] = len(all_listings)
    if not all_listings:
        known = report.get("market_notes") or {}
        if known:
            # Причина известна — незачем отправлять человека гадать.
            report["note"] = "ни одного лота. " + "; ".join(
                f"{market}: {notes[0]}" for market, notes in known.items()
            )
        else:
            report["note"] = (
                "площадки не вернули ни одного лота — проверьте токены и сеть "
                "(gift-cli probe)"
            )
        report["finished_at"] = utcnow().isoformat(timespec="seconds")
        save_report(report)
        return report

    # --- сохранение лотов ---
    with session_scope() as session:
        await refresh_fx(session)
        for dto in all_listings:
            gifts_service.upsert_listing(session, dto)
        seen: dict[Market, set[str]] = {}
        for dto in all_listings:
            seen.setdefault(dto.market, set()).add(dto.external_id)
        for market, ids in seen.items():
            gifts_service.deactivate_missing(session, market, ids)

    # --- оценка ---
    created = 0
    rejections: Counter[str] = Counter()
    for dto in all_listings:
        try:
            made, reasons = await evaluate_listing(dto, plan)
            created += made
            rejections.update(reasons)
        except Exception as exc:  # noqa: BLE001 - один лот не роняет скан
            log.exception("Ошибка оценки лота %s: %s", dto.external_id, exc)

    # --- разница цен между площадками ---
    # Считается отдельно от кандидатов: связка требует ручного
    # переноса подарка, поэтому она подсказка владельцу, а не заявка
    # на исполнение.
    if arbitrage.enabled():
        with session_scope() as session:
            spreads = arbitrage.find(session, all_listings)
        report["spreads"] = [s.as_dict() for s in spreads[:20]]
        if spreads:
            log.info(
                "Разница цен между площадками: %s связок, лучшая %.1f%%",
                len(spreads),
                float(spreads[0].net_roi) * 100,
            )

    report["candidates"] = created
    report["rejections"] = dict(rejections)
    report["finished_at"] = utcnow().isoformat(timespec="seconds")
    report["duration_sec"] = int((utcnow() - started).total_seconds())
    if not created and rejections:
        top = rejections.most_common(1)[0][0]
        report["note"] = (
            f"подходящих лотов нет, чаще всего: "
            f"{REJECTION_LABELS.get(top, top)}"
        )
    save_report(report)
    log.info(
        "Скан завершён: лотов %s, фактов %s, кандидатов %s",
        report["listings"],
        report["facts"],
        created,
    )
    return report


def _sale_hint(best: dict | None) -> dict | None:
    """Привести подсказку о продаже к виду, пригодному для JSON."""
    if not best:
        return None
    return {
        "market": best["market"],
        "sale_price": str(best["sale_price"].quantize(Decimal("1"))),
        "net_roi": f"{best['net_roi']:.1%}",
        "net_profit": str(best["net_profit"].quantize(Decimal("1"))),
        "listings": best["listings"],
        "note": best["note"],
    }


async def evaluate_listing(
    dto: ListingDTO, plan: list[dict]
) -> tuple[int, Counter[str]]:
    """Оценить лот по всем подходящим стратегиям.

    Returns:
        (сколько кандидатов создано, причины отсева)
    """
    from app.models import Strategy

    created = 0
    rejections: Counter[str] = Counter()
    with session_scope() as session:
        price_stars = marketdata.to_stars(session, dto.price, dto.currency)
        if price_stars is None or price_stars <= 0:
            return (0, rejections)

        snapshot = await snapshot_for_listing(session, dto)
        adapter = get_adapter(dto.market)
        is_official = (
            adapter.status_of(Capability.BUY) is CapabilityStatus.SUPPORTED
        )

        for item in plan:
            strategy = session.get(Strategy, item["id"])
            if strategy is None or not strategy.is_enabled:
                continue

            ok, reason = strategy_service.matches_filters(strategy, dto)
            if not ok:
                rejections["filters"] += 1
                continue
            ok, reason = strategy_service.price_in_range(strategy, price_stars)
            if not ok:
                rejections["price_range"] += 1
                continue
            ok, reason = strategy_service.can_open_position(session, strategy)
            if not ok:
                rejections["positions"] += 1
                log.debug("Стратегия %s: %s", strategy.name, reason)
                continue

            required = strategy_service.required_confidence(strategy)
            if not marketdata.confidence_at_least(snapshot.confidence, required):
                rejections["confidence"] += 1
                continue

            result = valuation.evaluate(
                session,
                buy_market=dto.market,
                buy_price=price_stars,
                # Продаём там же, где купили: кросс-рыночная сделка
                # не атомарна и требует отдельного подтверждения.
                sell_market=dto.market,
                snapshot=snapshot,
                is_official_api=is_official,
                target_markup=Decimal(strategy.sell_markup or 0),
            )

            if result.blockers:
                rejections["blockers"] += 1
                continue
            if result.net_roi < Decimal(strategy.min_roi):
                rejections["roi"] += 1
                continue
            if result.risk_score > strategy.max_risk:
                rejections["risk"] += 1
                continue

            # Где выгоднее продать: комиссии площадок различаются в
            # разы. Это подсказка — перенос подарка ручной, — поэтому
            # на отбор кандидата не влияет и живёт в обосновании.
            elsewhere = valuation.best_sale_market(
                session,
                buy_market=dto.market,
                buy_price=price_stars,
                collection=dto.gift.collection,
                model=dto.gift.model,
            )
            if elsewhere and elsewhere["net_roi"] > result.net_roi:
                result.reasons.append(elsewhere["note"])
            else:
                elsewhere = None

            gift = gifts_service.upsert_gift(session, dto.gift)
            exists = (
                session.query(Candidate)
                .filter_by(
                    strategy_id=strategy.id,
                    market=dto.market,
                    listing_external_id=dto.external_id,
                    state="pending",
                )
                .first()
            )
            if exists is not None:
                # Обновляем цену и оценку вместо создания дубликата.
                exists.price_stars = price_stars
                exists.price_native = dto.price
                exists.native_currency = dto.currency
                exists.fair_value_stars = result.fair_value
                exists.net_roi = result.net_roi
                exists.risk_score = result.risk_score
                exists.confidence = result.confidence
                exists.rationale = {
                    **result.as_dict(),
                    "market": snapshot.as_dict(),
                    "better_sale": _sale_hint(elsewhere),
                }
                exists.expires_at = utcnow() + CANDIDATE_TTL
                continue

            session.add(
                Candidate(
                    strategy_id=strategy.id,
                    gift_id=gift.id,
                    market=dto.market,
                    listing_external_id=dto.external_id,
                    price_stars=price_stars,
                    price_native=dto.price,
                    native_currency=dto.currency,
                    fair_value_stars=result.fair_value,
                    net_roi=result.net_roi,
                    risk_score=result.risk_score,
                    confidence=result.confidence,
                    rationale={
                        **result.as_dict(),
                        "market": snapshot.as_dict(),
                        "better_sale": _sale_hint(elsewhere),
                    },
                    state="pending",
                    expires_at=utcnow() + CANDIDATE_TTL,
                )
            )
            created += 1
            log.info(
                "Кандидат: %s на %s за %s Stars, ROI %.1f%%, риск %s",
                gifts_service.describe(gift),
                dto.market.value,
                price_stars,
                float(result.net_roi) * 100,
                result.risk_score,
            )
    return (created, rejections)


def expire_candidates(session: Session) -> int:
    """Пометить протухших кандидатов: их цена больше не актуальна."""
    now = utcnow()
    rows = (
        session.query(Candidate)
        .filter(Candidate.state == "pending", Candidate.expires_at < now)
        .all()
    )
    for row in rows:
        row.state = "expired"
    return len(rows)
