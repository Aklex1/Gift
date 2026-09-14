"""Репрайсер: лестница снижения цены до продажи.

Логика взята из требований к selling-циклу ТЗ: выставить, ждать,
снижать с шагом и cooldown, но никогда не опускаться ниже точки
безубыточности с учётом комиссий.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from app.db import session_scope
from app.enums import Market, PositionStatus, TradeMode
from app.models import Gift, Position, Strategy
from app.services import executor, marketdata, valuation
from app.services import strategy as strategy_service
from app.models import utcnow

log = logging.getLogger(__name__)


async def run_once() -> dict:
    """Один проход репрайсера по всем позициям.

    Действия:
        * позиции без листинга и без cooldown — выставить;
        * выставленные давно — снизить цену на шаг стратегии.
    """
    report = {"listed": 0, "repriced": 0, "skipped": 0}

    with session_scope() as session:
        plan_list: list[dict] = []
        plan_reprice: list[dict] = []

        for position in (
            session.query(Position)
            .filter(
                Position.status.in_(
                    [PositionStatus.HELD.value, PositionStatus.LISTED.value]
                )
            )
            .all()
        ):
            strategy = (
                session.get(Strategy, position.strategy_id)
                if position.strategy_id
                else None
            )
            if strategy is None or not strategy.is_enabled:
                report["skipped"] += 1
                continue

            mode = strategy_service.effective_mode(strategy)
            if mode is TradeMode.SAFE:
                # В SAFE система только рекомендует.
                report["skipped"] += 1
                continue

            # Подарок ещё под cooldown перепродажи.
            if position.resale_available_at and position.resale_available_at > utcnow():
                report["skipped"] += 1
                continue

            gift = session.get(Gift, position.gift_id)
            if gift is None:
                continue

            market = position.custody_market
            snapshot = marketdata.snapshot_for(
                session, collection=gift.collection, model=gift.model
            )

            if position.status is PositionStatus.HELD:
                price = valuation.suggested_list_price(
                    session,
                    market=market,
                    cost_basis=position.cost_basis,
                    snapshot=snapshot,
                    markup=Decimal(strategy.sell_markup or 0),
                    floor_ratio=Decimal(strategy.floor_ratio or 1),
                )
                plan_list.append({"position_id": position.id, "price": price, "mode": mode})
                continue

            # Уже выставлено — думаем о снижении.
            last = position.last_reprice_at or position.listed_at
            cooldown = dt.timedelta(hours=int(strategy.reprice_cooldown_h or 12))
            if last is not None and utcnow() - last < cooldown:
                report["skipped"] += 1
                continue

            current = Decimal(position.list_price or 0)
            if current <= 0:
                continue

            step = Decimal(strategy.reprice_step or 0)
            new_price = (current * (Decimal(1) - step)).quantize(Decimal("1"))

            floor = valuation.suggested_list_price(
                session,
                market=market,
                cost_basis=position.cost_basis,
                snapshot=snapshot,
                markup=Decimal(0),
                floor_ratio=Decimal(strategy.floor_ratio or 1),
            )
            if new_price <= floor:
                # Дальше снижать нельзя — уйдём в убыток.
                log.info(
                    "Позиция #%s: достигнут ценовой пол %s, снижение прекращено",
                    position.id,
                    floor,
                )
                report["skipped"] += 1
                continue

            plan_reprice.append(
                {"position_id": position.id, "price": new_price, "mode": mode}
            )

    # Исполняем вне транзакции БД.
    for item in plan_list:
        result = await executor.execute_list(
            item["position_id"], item["price"], actor="repricer", mode=item["mode"]
        )
        if result.get("ok"):
            report["listed"] += 1
        else:
            log.info("Позиция #%s не выставлена: %s", item["position_id"], result.get("detail"))

    for item in plan_reprice:
        result = await _reprice(item["position_id"], item["price"], item["mode"])
        if result.get("ok"):
            report["repriced"] += 1

    if report["listed"] or report["repriced"]:
        log.info("Репрайсер: выставлено %s, снижено %s", report["listed"], report["repriced"])
    return report


async def _reprice(position_id: int, new_price: Decimal, mode: TradeMode) -> dict:
    """Изменить цену уже выставленной позиции."""
    from app.adapters.base import AdapterError, Capability, OutcomeUnknown
    from app.adapters.registry import get_adapter

    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None or not position.list_external_id:
            return {"ok": False, "detail": "позиция не выставлена"}
        market = position.list_market
        try:
            executor.guard(mode, market, Capability.REPRICE)
        except executor.ExecutionBlocked as exc:
            return {"ok": False, "detail": str(exc)}
        external_id = position.list_external_id
        old_price = Decimal(position.list_price or 0)

    adapter = get_adapter(market)
    try:
        result = await adapter.reprice(
            external_id=external_id,
            new_price=new_price,
            idempotency_key=f"reprice-{position_id}-{new_price}",
        )
    except OutcomeUnknown as exc:
        log.warning("Позиция #%s: исход смены цены неизвестен: %s", position_id, exc)
        return {"ok": None, "detail": str(exc)}
    except AdapterError as exc:
        return {"ok": False, "detail": str(exc)}

    if result.ok:
        with session_scope() as session:
            position = session.get(Position, position_id)
            position.list_price = new_price
            position.last_reprice_at = utcnow()
            position.reprice_count = int(position.reprice_count or 0) + 1
        log.info(
            "Позиция #%s: цена снижена %s -> %s Stars", position_id, old_price, new_price
        )
        return {"ok": True, "detail": f"{old_price} -> {new_price}"}
    return {"ok": False, "detail": result.detail or "не удалось изменить цену"}
