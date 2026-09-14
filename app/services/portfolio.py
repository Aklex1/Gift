"""Портфель, PnL и сверка инвентаря."""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import Capability
from app.adapters.registry import get_adapter
from app.db import session_scope
from app.enums import Currency, Market, PositionStatus
from app.models import Gift, Position, Transaction, utcnow
from app.services import budget as budget_service
from app.services import gifts as gifts_service

log = logging.getLogger(__name__)

OPEN_STATES = [
    PositionStatus.HELD.value,
    PositionStatus.LOCKED.value,
    PositionStatus.LISTED.value,
]


def open_positions(session: Session, strategy_id: int | None = None) -> list[Position]:
    """Непроданные позиции."""
    query = session.query(Position).filter(Position.status.in_(OPEN_STATES))
    if strategy_id is not None:
        query = query.filter(Position.strategy_id == strategy_id)
    return query.order_by(Position.bought_at.desc()).all()


def pnl_summary(session: Session, since: dt.datetime | None = None) -> dict:
    """Сводка прибылей и убытков.

    Реализованный PnL считается только по закрытым сделкам;
    нереализованный — по себестоимости открытых позиций.
    """
    query = session.query(Position).filter(Position.status == PositionStatus.SOLD.value)
    if since is not None:
        query = query.filter(Position.sold_at >= since)
    closed = query.all()

    realized = sum((p.realized_pnl or Decimal(0) for p in closed), Decimal(0))
    revenue = sum((Decimal(p.sold_price or 0) for p in closed), Decimal(0))
    cost_closed = sum((p.cost_basis for p in closed), Decimal(0))

    opened = open_positions(session)
    locked_cost = sum((p.cost_basis for p in opened), Decimal(0))

    wins = [p for p in closed if (p.realized_pnl or Decimal(0)) > 0]
    hold_days = [
        (p.sold_at - p.bought_at).total_seconds() / 86400
        for p in closed
        if p.sold_at and p.bought_at
    ]

    return {
        "closed_count": len(closed),
        "open_count": len(opened),
        "realized_pnl": realized,
        "revenue": revenue,
        "cost_closed": cost_closed,
        "locked_cost": locked_cost,
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "roi": (realized / cost_closed) if cost_closed > 0 else Decimal(0),
        "avg_hold_days": (sum(hold_days) / len(hold_days)) if hold_days else 0.0,
    }


async def sync_inventory(market: Market = Market.TELEGRAM) -> dict:
    """Сверить портфель с реальным инвентарём площадки.

    Закрывает главный разрыв из ТЗ: canonical inventory и сверка продаж.
    Позиция, пропавшая из инвентаря и ранее выставленная, считается
    проданной по цене листинга.
    """
    adapter = get_adapter(market)
    if not adapter.supports(Capability.INVENTORY):
        return {"ok": False, "detail": f"{market.value}: инвентарь недоступен"}

    try:
        items = await adapter.inventory()
    except Exception as exc:  # noqa: BLE001
        log.warning("Сверка инвентаря %s не удалась: %s", market.value, exc)
        return {"ok": False, "detail": str(exc)}

    report = {"ok": True, "in_inventory": len(items), "sold": 0, "adopted": 0, "updated": 0}
    owned_slugs = {item.external_id for item in items}

    with session_scope() as session:
        # 1. Обновляем известные позиции.
        for position in open_positions(session):
            gift = session.get(Gift, position.gift_id)
            slug = gift.slug if gift else None
            if slug is None:
                continue

            if slug in owned_slugs:
                item = next(i for i in items if i.external_id == slug)
                raw = item.raw or {}
                # Запоминаем момент, с которого подарок можно перепродать.
                can_resell = raw.get("can_resell_at")
                if can_resell:
                    position.resale_available_at = dt.datetime.utcfromtimestamp(
                        int(can_resell)
                    )
                    if (
                        position.resale_available_at <= utcnow()
                        and position.status is PositionStatus.LOCKED
                    ):
                        position.status = PositionStatus.HELD
                elif position.status is PositionStatus.LOCKED:
                    position.status = PositionStatus.HELD

                # Синхронизируем факт выставления.
                if raw.get("is_listed") and item.price > 0:
                    position.status = PositionStatus.LISTED
                    position.list_price = item.price
                    position.list_external_id = slug
                    position.list_market = market
                report["updated"] += 1
                continue

            # 2. Позиции нет в инвентаре — вероятно, продана.
            if position.status is PositionStatus.LISTED and position.list_price:
                _close_as_sold(session, position, Decimal(position.list_price), market)
                report["sold"] += 1
            else:
                log.warning(
                    "Позиция #%s пропала из инвентаря без листинга — требуется ручная проверка",
                    position.id,
                )
                position.note = (position.note or "") + " | пропала из инвентаря"

        # 3. Подарки в инвентаре, которых нет в портфеле.
        known = {
            g.slug
            for g in session.query(Gift)
            .join(Position, Position.gift_id == Gift.id)
            .filter(Position.status.in_(OPEN_STATES))
            .all()
            if g.slug
        }
        for item in items:
            if item.external_id in known:
                continue
            gift = gifts_service.upsert_gift(session, item.gift)
            exists = (
                session.query(Position)
                .filter(
                    Position.gift_id == gift.id, Position.status.in_(OPEN_STATES)
                )
                .first()
            )
            if exists is not None:
                continue
            # Подарок получен вне бота: берём под управление с нулевой
            # себестоимостью, чтобы он попал в репрайсинг.
            session.add(
                Position(
                    gift_id=gift.id,
                    status=(
                        PositionStatus.LISTED
                        if (item.raw or {}).get("is_listed")
                        else PositionStatus.HELD
                    ),
                    custody_market=market,
                    buy_market=market,
                    buy_price=Decimal(0),
                    buy_currency=Currency.STARS,
                    bought_at=utcnow(),
                    list_price=item.price if item.price > 0 else None,
                    list_external_id=item.external_id if item.price > 0 else None,
                    note="получен вне бота, себестоимость неизвестна",
                )
            )
            report["adopted"] += 1

    log.info("Сверка инвентаря %s: %s", market.value, report)
    return report


def _close_as_sold(
    session: Session, position: Position, price: Decimal, market: Market
) -> None:
    """Закрыть позицию как проданную и вернуть выручку в бюджет."""
    from app.services.valuation import get_fees, net_proceeds_from

    # Цена здесь — в валюте площадки (list_price ставит репрайсер), и
    # get_fees отдаёт сетевую комиссию в той же валюте. Приводить к
    # Stars тут нечего: обе величины уже согласованы.
    fees = get_fees(session, market)
    proceeds = net_proceeds_from(price, fees)

    position.status = PositionStatus.SOLD
    position.sold_price = price
    position.sold_fee = price - proceeds
    position.sold_at = utcnow()

    session.add(
        Transaction(
            position_id=position.id,
            market=market,
            kind="sell",
            amount=price,
            currency=Currency.STARS,
            fee=position.sold_fee,
        )
    )

    # Выручка возвращается в оборот той же стратегии.
    if position.strategy_id:
        from app.models import Strategy

        strategy = session.get(Strategy, position.strategy_id)
        if strategy and strategy.budget_id:
            budget_service.credit(
                session,
                strategy.budget_id,
                proceeds,
                note=f"продажа позиции #{position.id}",
            )

    pnl = position.realized_pnl
    log.info(
        "Позиция #%s продана за %s Stars, PnL %s",
        position.id,
        price,
        pnl if pnl is not None else "?",
    )
