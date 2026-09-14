"""Исполнитель сделок: покупка, выставление, снятие.

Порядок для каждой покупки жёстко фиксирован:

    1. Проверить kill switch и режим.
    2. Проверить, что площадка имеет право на эту операцию.
    3. Создать намерение (идемпотентность).
    4. Атомарно зарезервировать бюджет.
    5. Отправить внешний вызов.
    6. Списать резерв или освободить его; при неизвестном исходе —
       UNKNOWN и сверка, без повторной покупки.

Ни один шаг нельзя пропустить: резерв всегда создаётся ДО внешнего
вызова, иначе параллельные стратегии могут превысить общий лимит.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import (
    AdapterError,
    Capability,
    ExecutionResult,
    GiftRef,
    OutcomeUnknown,
    RateLimited,
)
from app.adapters.registry import get_adapter
from app.config import settings
from app.db import session_scope
from app.enums import (
    Currency,
    IntentKind,
    IntentStatus,
    Market,
    PositionStatus,
    TradeMode,
)
from app.models import AuditLog, Candidate, Gift, Intent, Position, Strategy, Transaction, utcnow
from app.services import budget as budget_service
from app.services import runtime
from app.services import saga
from app.services import strategy as strategy_service

log = logging.getLogger(__name__)


class ExecutionBlocked(Exception):
    """Исполнение запрещено предохранителем."""


# ----------------------------------------------------------------------
# Предохранители
# ----------------------------------------------------------------------
def check_kill_switch() -> None:
    """Глобальный аварийный стоп.

    Raises:
        ExecutionBlocked: если kill switch включён.
    """
    if runtime.kill_switch():
        raise ExecutionBlocked(
            "Активен аварийный стоп: все торговые операции запрещены"
        )


def check_auto_allowed(market: Market, capability: Capability) -> None:
    """Проверить право площадки работать автономно.

    В AUTO допускаются только официальные API из белого списка:
    приватные коннекторы без SLA автономно торговать не могут.
    """
    adapter = get_adapter(market)
    if not adapter.is_auto_safe(capability):
        raise ExecutionBlocked(
            f"{market.value}: операция {capability.value} не разрешена "
            f"в автономном режиме (статус {adapter.status_of(capability).value}; "
            f"для приватных API нужно разрешить автономный режим в панели)"
        )
    if market.value not in runtime.auto_markets():
        raise ExecutionBlocked(
            f"{market.value}: боевой режим выключен, автономная торговля невозможна"
        )


def guard(mode: TradeMode, market: Market, capability: Capability) -> None:
    """Полный набор проверок перед write-вызовом.

    Порядок от общего к частному, чтобы сообщение называло настоящую
    причину: сначала глобальные предохранители, затем конкретная
    площадка и операция.
    """
    check_kill_switch()

    if mode is TradeMode.SAFE:
        raise ExecutionBlocked(
            "Режим SAFE: система только рекомендует, торговые операции запрещены"
        )

    if not runtime.write_enabled(market):
        raise ExecutionBlocked(
            f"{market.value}: боевой режим выключен "
            f"(включается в панели, раздел «Торговля»)"
        )

    adapter = get_adapter(market)
    if not adapter.supports(capability):
        raise ExecutionBlocked(
            f"{market.value}: операция {capability.value} недоступна "
            f"(не описана в контракте markets/{market.value}.json "
            f"или нет доступа к площадке)"
        )

    if mode is TradeMode.AUTO:
        check_auto_allowed(market, capability)


# ----------------------------------------------------------------------
# Покупка
# ----------------------------------------------------------------------
async def execute_buy(
    candidate_id: int, *, actor: str = "system", mode: TradeMode | None = None
) -> dict:
    """Купить подарок по кандидату.

    Args:
        candidate_id: id кандидата из сканера.
        actor: кто инициировал (owner / system).
        mode: переопределить режим (подтверждение владельца = SEMI).

    Returns:
        Сводка результата.
    """
    # --- 1. Подготовка и проверки ---
    with session_scope() as session:
        candidate = session.get(Candidate, candidate_id)
        if candidate is None:
            return {"ok": False, "detail": "кандидат не найден"}
        if candidate.state != "pending":
            return {"ok": False, "detail": f"кандидат уже обработан: {candidate.state}"}
        if candidate.expires_at < utcnow():
            candidate.state = "expired"
            return {"ok": False, "detail": "цена устарела, нужен новый скан"}

        strategy = session.get(Strategy, candidate.strategy_id)
        if strategy is None:
            return {"ok": False, "detail": "стратегия не найдена"}

        effective = mode or strategy_service.effective_mode(strategy)
        market = candidate.market

        try:
            guard(effective, market, Capability.BUY)
        except ExecutionBlocked as exc:
            return {"ok": False, "detail": str(exc)}

        ok, reason = strategy_service.can_open_position(session, strategy)
        if not ok:
            return {"ok": False, "detail": reason}

        # Цена в валюте площадки — именно её ждёт адаптер.
        # Для Portals/MRKT это TON; передать сюда Stars значило бы
        # промахнуться на два порядка.
        native_price = Decimal(
            candidate.price_native
            if candidate.price_native is not None
            else candidate.price_stars
        )
        native_currency = candidate.native_currency or Currency.STARS
        price_stars = Decimal(candidate.price_stars)
        external_id = candidate.listing_external_id

        # Предохранитель на сумму сделки в валюте площадки.
        cap = runtime.trade_cap(market)
        if cap is not None and native_price > cap:
            return {
                "ok": False,
                "detail": (
                    f"цена {native_price} {native_currency.value} выше лимита "
                    f"{cap} для {market.value}"
                ),
            }

        # --- 2. Намерение (идемпотентность) ---
        intent = saga.plan(
            session,
            kind=IntentKind.BUY,
            market=market,
            mode=effective,
            external_id=external_id,
            price=native_price,
            currency=native_currency,
            strategy_id=strategy.id,
            gift_id=candidate.gift_id,
            decision=candidate.rationale or {},
        )
        if intent.status is not IntentStatus.PLANNED:
            return {
                "ok": False,
                "detail": f"операция уже выполнялась, статус: {intent.status}",
                "intent_id": intent.id,
            }

        # --- 3. Резерв бюджета ДО внешнего вызова ---
        if not strategy.budget_id:
            return {"ok": False, "detail": "у стратегии нет бюджета"}
        # Бюджет ведётся в своей валюте: приводим цену к ней.
        budget_currency = _budget_currency(session, strategy.budget_id)
        reserve_amount = _convert(
            session, native_price, native_currency, budget_currency
        )
        if reserve_amount is None:
            saga.transition(
                session,
                intent,
                IntentStatus.CANCELLED,
                error=f"нет курса {native_currency.value}->{budget_currency.value}",
            )
            return {
                "ok": False,
                "detail": (
                    f"не удалось пересчитать цену из {native_currency.value} "
                    f"в валюту бюджета {budget_currency.value}"
                ),
            }
        try:
            reservation = budget_service.reserve(
                session,
                budget_id=strategy.budget_id,
                amount=reserve_amount,
                intent_id=intent.id,
                note=f"покупка {external_id}",
            )
        except budget_service.BudgetError as exc:
            saga.transition(session, intent, IntentStatus.CANCELLED, error=str(exc))
            return {"ok": False, "detail": str(exc)}

        saga.transition(session, intent, IntentStatus.RESERVED)
        candidate.state = "approved"
        candidate.intent_id = intent.id

        intent_id = intent.id
        reservation_id = reservation.id
        gift_id = candidate.gift_id

        session.add(
            AuditLog(
                actor=actor,
                action="buy.start",
                target=f"candidate:{candidate_id}",
                payload={
                    "price": str(native_price),
                    "currency": native_currency.value,
                    "price_stars": str(price_stars),
                    "market": market.value,
                    "mode": effective.value,
                },
            )
        )

    # --- 4. Внешний вызов (вне транзакции БД) ---
    adapter = get_adapter(market)
    idempotency_key = f"buy-{intent_id}"
    result: ExecutionResult | None = None
    unknown_detail: str | None = None

    with session_scope() as session:
        intent = session.get(Intent, intent_id)
        saga.transition(session, intent, IntentStatus.SUBMITTED)

    try:
        result = await adapter.buy(
            external_id=external_id,
            expected_price=native_price,
            idempotency_key=idempotency_key,
        )
    except OutcomeUnknown as exc:
        unknown_detail = str(exc)
    except RateLimited as exc:
        unknown_detail = f"лимит запросов: {exc}"
    except AdapterError as exc:
        result = ExecutionResult(ok=False, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("Непредвиденная ошибка покупки: %s", exc)
        unknown_detail = f"непредвиденная ошибка: {exc}"

    # --- 5. Разбор исхода ---
    with session_scope() as session:
        intent = session.get(Intent, intent_id)
        candidate = session.get(Candidate, candidate_id)

        if unknown_detail is not None:
            # Исход неизвестен: деньги могли уйти. Резерв НЕ освобождаем,
            # повторную покупку НЕ делаем — ждём сверки.
            saga.mark_unknown(session, intent, unknown_detail)
            return {
                "ok": None,
                "detail": (
                    "Исход неизвестен, покупка не повторяется. "
                    "Запущена сверка с площадкой."
                ),
                "intent_id": intent_id,
            }

        assert result is not None
        if result.ok is None:
            saga.mark_unknown(session, intent, result.detail or "неопределённый ответ")
            return {"ok": None, "detail": "исход неизвестен, запущена сверка", "intent_id": intent_id}

        if not result.ok:
            saga.transition(
                session, intent, IntentStatus.FAILED, error=result.detail or "отказ"
            )
            budget_service.release(session, reservation_id, reason=result.detail or "отказ")
            if candidate is not None:
                candidate.state = "rejected"
            return {"ok": False, "detail": result.detail or "покупка не прошла"}

        # Успех: списываем резерв и создаём позицию.
        executed = result.executed_price or native_price
        executed_currency = result.currency or native_currency
        saga.transition(
            session,
            intent,
            IntentStatus.CONFIRMED,
            external_ref=result.external_ref,
            executed_price=executed,
        )
        settled = (
            _convert(session, executed, executed_currency, budget_currency)
            or reserve_amount
        )
        budget_service.settle(session, reservation_id, actual_amount=settled)

        position = _create_position(
            session,
            gift_id=gift_id,
            strategy_id=intent.strategy_id,
            market=market,
            price=executed,
            currency=executed_currency,
            intent_id=intent_id,
        )
        session.add(
            Transaction(
                intent_id=intent_id,
                position_id=position.id,
                market=market,
                kind="buy",
                amount=executed,
                currency=executed_currency,
                external_ref=result.external_ref,
            )
        )
        if candidate is not None:
            candidate.state = "executed"

        return {
            "ok": True,
            "detail": f"куплено за {executed} {executed_currency.value}",
            "intent_id": intent_id,
            "position_id": position.id,
        }


def _budget_currency(session: Session, budget_id: int | None) -> Currency:
    """Валюта, в которой ведётся бюджет стратегии."""
    from app.models import Budget

    budget = session.get(Budget, budget_id) if budget_id else None
    return budget.currency if budget else Currency.STARS


def _convert(
    session: Session, amount: Decimal, source: Currency, target: Currency
) -> Decimal | None:
    """Перевести сумму между валютами по последнему FX-снапшоту."""
    from app.services import marketdata

    if source is target:
        return amount
    if target is Currency.STARS:
        return marketdata.to_stars(session, amount, source)
    stars = marketdata.to_stars(session, amount, source)
    if stars is None:
        return None
    rate = marketdata.latest_fx(session, target, Currency.STARS)
    if rate is None and target is Currency.TON:
        rate = marketdata.DEFAULT_STARS_PER_TON
    if not rate or rate <= 0:
        return None
    return stars / rate


def _create_position(
    session: Session,
    *,
    gift_id: int,
    strategy_id: int | None,
    market: Market,
    price: Decimal,
    intent_id: int,
    currency: Currency = Currency.STARS,
) -> Position:
    """Создать позицию в портфеле после успешной покупки."""
    position = Position(
        gift_id=gift_id,
        strategy_id=strategy_id,
        status=PositionStatus.LOCKED,
        custody_market=market,
        buy_market=market,
        buy_price=price,
        buy_currency=currency,
        bought_at=utcnow(),
        buy_intent_id=intent_id,
    )
    session.add(position)
    session.flush()
    log.info(
        "Открыта позиция #%s: подарок %s за %s %s",
        position.id,
        gift_id,
        price,
        currency.value,
    )
    return position


# ----------------------------------------------------------------------
# Выставление на продажу
# ----------------------------------------------------------------------
async def execute_list(
    position_id: int, price: Decimal, *, actor: str = "system", mode: TradeMode | None = None
) -> dict:
    """Выставить позицию на продажу."""
    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None:
            return {"ok": False, "detail": "позиция не найдена"}
        if position.status is PositionStatus.SOLD:
            return {"ok": False, "detail": "позиция уже продана"}

        # Подарок может быть под cooldown после покупки.
        if position.resale_available_at and position.resale_available_at > utcnow():
            return {
                "ok": False,
                "detail": (
                    f"перепродажа доступна с {position.resale_available_at:%d.%m.%Y %H:%M} UTC"
                ),
            }

        strategy = (
            session.get(Strategy, position.strategy_id) if position.strategy_id else None
        )
        effective = mode or (
            strategy_service.effective_mode(strategy) if strategy else TradeMode.SEMI
        )
        market = position.custody_market

        try:
            guard(effective, market, Capability.LIST)
        except ExecutionBlocked as exc:
            return {"ok": False, "detail": str(exc)}

        gift = session.get(Gift, position.gift_id)
        intent = saga.plan(
            session,
            kind=IntentKind.LIST,
            market=market,
            mode=effective,
            external_id=gift.slug if gift else None,
            price=price,
            strategy_id=position.strategy_id,
            gift_id=position.gift_id,
        )
        if intent.status is not IntentStatus.PLANNED:
            return {"ok": False, "detail": f"намерение уже в статусе {intent.status}"}

        saga.transition(session, intent, IntentStatus.RESERVED)
        saga.transition(session, intent, IntentStatus.SUBMITTED)
        intent_id = intent.id
        gift_ref = GiftRef(
            collection=gift.collection if gift else "",
            slug=gift.slug if gift else None,
            attributes={"msg_id": (gift.attributes or {}).get("msg_id") if gift else None},
        )

    adapter = get_adapter(market)
    try:
        result = await adapter.list_for_sale(
            gift_ref=gift_ref, price=price, idempotency_key=f"list-{intent_id}"
        )
    except OutcomeUnknown as exc:
        with session_scope() as session:
            saga.mark_unknown(session, session.get(Intent, intent_id), str(exc))
        return {"ok": None, "detail": "исход неизвестен, запущена сверка"}
    except AdapterError as exc:
        with session_scope() as session:
            saga.transition(
                session, session.get(Intent, intent_id), IntentStatus.FAILED, error=str(exc)
            )
        return {"ok": False, "detail": str(exc)}

    with session_scope() as session:
        intent = session.get(Intent, intent_id)
        position = session.get(Position, position_id)
        if result.ok:
            saga.transition(
                session,
                intent,
                IntentStatus.CONFIRMED,
                external_ref=result.external_ref,
                executed_price=price,
            )
            position.status = PositionStatus.LISTED
            position.list_market = market
            position.list_price = price
            position.list_external_id = result.external_ref
            position.listed_at = utcnow()
            position.last_reprice_at = utcnow()
            currency = get_adapter(market).native_currency
            return {"ok": True, "detail": f"выставлено за {price} {currency.value}"}

        saga.transition(session, intent, IntentStatus.FAILED, error=result.detail)
        return {"ok": False, "detail": result.detail or "не удалось выставить"}


async def execute_cancel(position_id: int, *, actor: str = "system") -> dict:
    """Снять позицию с продажи."""
    with session_scope() as session:
        position = session.get(Position, position_id)
        if position is None or not position.list_external_id:
            return {"ok": False, "detail": "позиция не выставлена"}
        market = position.list_market
        try:
            guard(TradeMode.SEMI, market, Capability.CANCEL)
        except ExecutionBlocked as exc:
            return {"ok": False, "detail": str(exc)}
        external_id = position.list_external_id

    adapter = get_adapter(market)
    try:
        result = await adapter.cancel(
            external_id=external_id, idempotency_key=f"cancel-{position_id}"
        )
    except AdapterError as exc:
        return {"ok": False, "detail": str(exc)}

    if result.ok:
        with session_scope() as session:
            position = session.get(Position, position_id)
            position.status = PositionStatus.HELD
            position.list_external_id = None
            position.list_price = None
        return {"ok": True, "detail": "снято с продажи"}
    return {"ok": False, "detail": result.detail or "не удалось снять"}
