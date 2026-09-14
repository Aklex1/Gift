"""Движок стратегий: независимые правила отбора со своими бюджетами."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import ListingDTO
from app.config import settings
from app.enums import Confidence, Currency, Market, TradeMode
from app.models import Budget, Position, Strategy
from app.services.budget import get_or_create_budget

log = logging.getLogger(__name__)


def create_strategy(
    session: Session,
    *,
    name: str,
    markets: list[str] | None = None,
    collections: list[str] | None = None,
    min_roi: Decimal | None = None,
    max_price_stars: Decimal | None = None,
    budget_cap: Decimal | None = None,
    mode: TradeMode = TradeMode.SAFE,
) -> Strategy:
    """Создать стратегию с собственным бюджетом."""
    existing = session.query(Strategy).filter_by(name=name).one_or_none()
    if existing is not None:
        return existing

    budget = get_or_create_budget(
        session,
        name=f"budget:{name}",
        hard_cap=budget_cap if budget_cap is not None else Decimal("0"),
    )
    strategy = Strategy(
        name=name,
        is_enabled=False,
        mode=mode,
        budget_id=budget.id,
        markets=markets or [Market.TELEGRAM.value],
        collections=collections or [],
        models=[],
        backdrops=[],
        symbols=[],
        min_roi=min_roi if min_roi is not None else Decimal(str(settings.min_roi)),
        max_price_stars=max_price_stars,
        max_open_positions=settings.max_open_positions,
    )
    session.add(strategy)
    session.flush()
    log.info("Создана стратегия %r (бюджет %s)", name, budget.hard_cap)
    return strategy


def active_strategies(session: Session) -> list[Strategy]:
    """Включённые стратегии в порядке приоритета."""
    return (
        session.query(Strategy)
        .filter(Strategy.is_enabled.is_(True))
        .order_by(Strategy.priority.desc(), Strategy.id.asc())
        .all()
    )


def matches_filters(strategy: Strategy, dto: ListingDTO) -> tuple[bool, str]:
    """Проходит ли лот фильтры стратегии.

    Returns:
        (подходит, причина отказа)
    """
    markets = [str(m).lower() for m in (strategy.markets or [])]
    if markets and dto.market.value not in markets:
        return (False, f"площадка {dto.market.value} не в списке стратегии")

    def _match(values: list, actual: str | None) -> bool:
        """Пустой список фильтра означает «любое значение»."""
        if not values:
            return True
        if actual is None:
            return False
        wanted = {str(v).strip().lower() for v in values}
        return actual.strip().lower() in wanted

    if not _match(strategy.collections or [], dto.gift.collection):
        return (False, f"коллекция {dto.gift.collection!r} не в фильтре")
    if not _match(strategy.models or [], dto.gift.model):
        return (False, f"модель {dto.gift.model!r} не в фильтре")
    if not _match(strategy.backdrops or [], dto.gift.backdrop):
        return (False, f"фон {dto.gift.backdrop!r} не в фильтре")
    if not _match(strategy.symbols or [], dto.gift.symbol):
        return (False, f"символ {dto.gift.symbol!r} не в фильтре")

    return (True, "")


def price_in_range(strategy: Strategy, price_stars: Decimal) -> tuple[bool, str]:
    """Укладывается ли цена в ценовой коридор стратегии."""
    if strategy.min_price_stars and price_stars < Decimal(strategy.min_price_stars):
        return (False, f"цена {price_stars} ниже минимума стратегии")
    if strategy.max_price_stars and price_stars > Decimal(strategy.max_price_stars):
        return (False, f"цена {price_stars} выше максимума стратегии")
    max_trade = Decimal(settings.max_trade_stars or 0)
    if max_trade > 0 and price_stars > max_trade:
        return (False, f"цена {price_stars} выше глобального лимита сделки {max_trade}")
    return (True, "")


def open_positions_count(session: Session, strategy_id: int) -> int:
    """Сколько позиций стратегии сейчас не продано."""
    from app.enums import PositionStatus

    return (
        session.query(Position)
        .filter(
            Position.strategy_id == strategy_id,
            Position.status.in_(
                [
                    PositionStatus.HELD.value,
                    PositionStatus.LOCKED.value,
                    PositionStatus.LISTED.value,
                ]
            ),
        )
        .count()
    )


def can_open_position(session: Session, strategy: Strategy) -> tuple[bool, str]:
    """Есть ли у стратегии место под новую позицию."""
    count = open_positions_count(session, strategy.id)
    if count >= strategy.max_open_positions:
        return (
            False,
            f"достигнут лимит открытых позиций стратегии: {count}/{strategy.max_open_positions}",
        )
    global_cap = settings.max_open_positions
    if global_cap > 0:
        from app.enums import PositionStatus

        total = (
            session.query(Position)
            .filter(
                Position.status.in_(
                    [
                        PositionStatus.HELD.value,
                        PositionStatus.LOCKED.value,
                        PositionStatus.LISTED.value,
                    ]
                )
            )
            .count()
        )
        if total >= global_cap:
            return (False, f"достигнут глобальный лимит позиций: {total}/{global_cap}")
    return (True, "")


def required_confidence(strategy: Strategy) -> Confidence:
    """Минимально допустимое качество данных для стратегии."""
    return strategy.min_confidence


def effective_mode(strategy: Strategy) -> TradeMode:
    """Режим стратегии, ограниченный глобальной настройкой.

    Стратегия не может быть «смелее» глобального режима: если система
    в SAFE, ни одна стратегия не уйдёт в AUTO.
    """
    order = {TradeMode.SAFE: 0, TradeMode.SEMI: 1, TradeMode.AUTO: 2}
    from app.services import runtime

    strategy_mode = strategy.mode
    global_mode = runtime.mode()
    return strategy_mode if order[strategy_mode] <= order[global_mode] else global_mode


def seed_default_strategy(session: Session) -> Strategy:
    """Создать безопасную стартовую стратегию.

    Выключена, режим SAFE, только официальный маркет Telegram —
    чтобы после установки ничего не произошло само собой.
    """
    strategy = create_strategy(
        session,
        name="telegram-floor",
        markets=[Market.TELEGRAM.value],
        min_roi=Decimal(str(settings.min_roi)),
        mode=TradeMode.SAFE,
    )
    budget: Budget | None = (
        session.get(Budget, strategy.budget_id) if strategy.budget_id else None
    )
    if budget is not None and Decimal(budget.hard_cap) == 0:
        budget.currency = Currency.STARS
    return strategy
