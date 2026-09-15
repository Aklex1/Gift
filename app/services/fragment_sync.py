"""Сбор состоявшихся продаж с Fragment.

Зачем это нужно. Скорость продаж — единственная величина, которую бот
до сих пор не мог узнать ниоткуда: MTProto отдаёт только собственные
операции, приватные API площадок истории чужих сделок не показывают. В
итоге почти каждый расчёт получал надбавку к риску «скорость продаж
неизвестна», а срок продажи оставался неизвестным. Fragment показывает
цену и время каждой сделки — этого достаточно.

Что здесь не делается. Активные лоты Fragment в базу не пишутся, и это
намеренно: купить там бот не может, а лот в таблице выглядел бы как
предложение к покупке. Сохраняются только факты продаж.

Сколько это стоит. Один запрос на пару «коллекция + модель». Пары
берутся те, что бот реально встречает, обновляются по кругу от самой
залежавшейся, и за проход их не больше потолка — чтобы редкий обход
всей базы не превращался в шквал запросов к чужому сайту.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy.orm import Session

from app.adapters.fragment import FilterIgnored
from app.adapters.registry import get_adapter
from app.db import session_scope
from app.enums import Capability, Market
from app.models import Candidate, Gift, Listing, utcnow
from app.services import marketdata, store

log = logging.getLogger(__name__)

#: Какие пары считаем встреченными: что попадалось за последние сутки.
SEEN_WINDOW = dt.timedelta(days=1)
#: Сколько пар обновляем за один проход.
MAX_PAIRS = 20
#: Насколько долго собранная по паре история считается свежей.
FRESH_FOR = dt.timedelta(hours=6)
#: Где хранится время последнего обхода по каждой паре.
KEY_STATE = "FRAGMENT_SYNC_STATE"
#: Сколько записей о времени обхода храним: остальное — мусор.
STATE_LIMIT = 400


def _pair_key(collection: str, model: str | None) -> str:
    return f"{collection}|{model or ''}"


def _state() -> dict[str, str]:
    """Когда каждую пару обновляли в последний раз."""
    raw = store.get(KEY_STATE)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, str]) -> None:
    """Сохранить времена обхода, отбросив самые старые записи."""
    if len(state) > STATE_LIMIT:
        keep = sorted(state.items(), key=lambda kv: kv[1], reverse=True)
        state = dict(keep[:STATE_LIMIT])
    store.set(KEY_STATE, json.dumps(state))


def pairs_of_interest(session: Session) -> list[tuple[str, str | None]]:
    """Пары «коллекция + модель», которые бот реально встречает.

    Это активные лоты и недавние кандидаты: обходить все коллекции
    Fragment незачем, история нужна ровно там, где принимается решение.
    """
    since = utcnow() - SEEN_WINDOW
    pairs: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()

    rows = (
        session.query(Gift.collection, Gift.model)
        .join(Listing, Listing.gift_id == Gift.id)
        .filter(Listing.is_active.is_(True), Listing.seen_at >= since)
        .distinct()
        .all()
    )
    rows += (
        session.query(Gift.collection, Gift.model)
        .join(Candidate, Candidate.gift_id == Gift.id)
        .filter(Candidate.created_at >= since)
        .distinct()
        .all()
    )

    for collection, model in rows:
        if not collection:
            continue
        # Помимо модели берём коллекцию целиком: по ней продаж больше,
        # и скорость получается по более широкой выборке.
        for pair in ((collection, model), (collection, None)):
            if pair in seen:
                continue
            seen.add(pair)
            pairs.append(pair)
    return pairs


def due(pairs: list[tuple[str, str | None]], state: dict[str, str]) -> list[tuple[str, str | None]]:
    """Что пора обновить: сначала то, что дольше всех не трогали."""
    now = utcnow()
    stale: list[tuple[dt.datetime, tuple[str, str | None]]] = []
    for pair in pairs:
        raw = state.get(_pair_key(*pair))
        if raw:
            try:
                last = dt.datetime.fromisoformat(raw)
            except ValueError:
                last = dt.datetime.min
        else:
            last = dt.datetime.min
        if now - last < FRESH_FOR:
            continue
        stale.append((last, pair))

    stale.sort(key=lambda item: item[0])
    return [pair for _, pair in stale[:MAX_PAIRS]]


async def sync() -> dict:
    """Обновить историю продаж по очередной порции пар.

    Returns:
        Что удалось собрать: сколько пар обошли и сколько новых
        сделок записали. Ошибка одной пары не останавливает остальные.
    """
    adapter = get_adapter(Market.FRAGMENT)
    if not adapter.supports(Capability.HISTORY):
        return {"ok": False, "detail": "история Fragment недоступна"}

    with session_scope() as session:
        pairs = pairs_of_interest(session)
    state = _state()
    batch = due(pairs, state)
    if not batch:
        return {"ok": True, "pairs": 0, "sales": 0, "known": len(pairs)}

    saved = 0
    failed: list[str] = []
    now = utcnow().isoformat()
    for collection, model in batch:
        try:
            sales = await adapter.history(collection=collection, model=model)
        except FilterIgnored:
            # Название модели площадке неизвестно — своего среза по ней
            # нет. Помечаем обойдённой, чтобы не долбиться каждый проход.
            state[_pair_key(collection, model)] = now
            continue
        except Exception as exc:  # noqa: BLE001 - источник необязательный
            failed.append(f"{collection}/{model or '—'}: {type(exc).__name__}")
            continue

        if sales:
            with session_scope() as session:
                saved += marketdata.record_facts(session, sales, Market.FRAGMENT)
        state[_pair_key(collection, model)] = now

    _save_state(state)
    report = {
        "ok": True,
        "pairs": len(batch),
        "sales": saved,
        "known": len(pairs),
    }
    if failed:
        report["failed"] = failed[:5]
    return report
