"""Telegram-бот управления: owner-only консоль.

Доступ fail-closed: пустой список OWNER_IDS означает, что бот не
отвечает никому. Любая торговая операция требует явного подтверждения,
кроме режима AUTO для площадок из белого списка.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.adapters.base import Capability
from app.adapters.registry import capability_matrix, get_adapter
from app.bot import keyboards as kb
from app.config import settings
from app.db import session_scope
from app.enums import (
    Confidence,
    Market,
    PositionStatus,
    TradeMode,
    display_currency,
)
from app.logging_conf import setup_logging
from app.models import AuditLog, Budget, Candidate, Gift, Position, Strategy, utcnow
from app.services import budget as budget_service
from app.services import runtime
from app.services import secrets
from app.services import executor, portfolio
from app.services import gifts as gifts_service
from app.services import strategy as strategy_service

log = logging.getLogger(__name__)

dp = Dispatcher()

#: Ожидание ввода: user_id -> (действие, параметр)
_pending_input: dict[int, tuple[str, int]] = {}


# ----------------------------------------------------------------------
# Доступ
# ----------------------------------------------------------------------
def owner_ids() -> list[int]:
    """Список владельцев: из панели, иначе из .env."""
    raw = secrets.resolve("OWNER_IDS", settings.owner_ids)
    out: list[int] = []
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


def is_owner(user_id: int | None) -> bool:
    """Fail-closed проверка владельца."""
    owners = owner_ids()
    if not owners:
        return False
    return user_id in owners


async def deny(event: Message | CallbackQuery) -> None:
    """Отказать в доступе и записать попытку в аудит."""
    user = event.from_user
    log.warning("Отказано в доступе: id=%s", getattr(user, "id", "?"))
    with session_scope() as session:
        session.add(
            AuditLog(
                actor=str(getattr(user, "id", "?")),
                action="access.denied",
                ok=False,
                payload={"username": getattr(user, "username", None)},
            )
        )
    if isinstance(event, Message):
        await event.answer("Доступ запрещён.")
    else:
        await event.answer("Доступ запрещён.", show_alert=True)


# ----------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Приветствие и главное меню."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    mode_value = runtime.mode().value
    await message.answer(
        "<b>Gift — торговый бот подарков Telegram</b>\n\n"
        f"Режим: <b>{mode_value.upper()}</b>\n"
        f"Аварийный стоп: <b>{'ВКЛЮЧЁН' if runtime.kill_switch() else 'выключен'}</b>\n\n"
        "SAFE — только рекомендации.\n"
        "SEMI — покупка после вашего подтверждения.\n"
        "AUTO — автономно, только официальный API Telegram из белого списка.\n\n"
        "Ваш Telegram ID: <code>"
        f"{message.from_user.id if message.from_user else '?'}</code>",
        reply_markup=kb.main_menu(),
    )


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    """Показать свой Telegram ID — нужно для заполнения OWNER_IDS."""
    await message.answer(
        f"Ваш Telegram ID: <code>{message.from_user.id if message.from_user else '?'}</code>"
    )


@dp.message(F.text == "🛑 СТОП")
@dp.message(Command("stop"))
async def cmd_kill(message: Message) -> None:
    """Аварийный стоп: мгновенно запрещает все write-операции."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    enabled = not runtime.kill_switch()
    runtime.set_kill_switch(enabled, actor=str(message.from_user.id))
    await message.answer(
        f"🛑 Аварийный стоп <b>{'ВКЛЮЧЁН' if enabled else 'выключен'}</b>.\n"
        + (
            "Все торговые операции заблокированы — во всех процессах."
            if enabled
            else "Торговля снова разрешена в пределах режима и лимитов."
        )
    )


# ----------------------------------------------------------------------
# Кандидаты
# ----------------------------------------------------------------------
@dp.message(F.text == "🔎 Кандидаты")
@dp.message(Command("candidates"))
async def cmd_candidates(message: Message) -> None:
    """Показать найденные возможности."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return

    with session_scope() as session:
        rows = (
            session.query(Candidate)
            .filter(Candidate.state == "pending", Candidate.expires_at > utcnow())
            .order_by(Candidate.net_roi.desc())
            .limit(10)
            .all()
        )
        if not rows:
            await message.answer(
                "Активных кандидатов нет.\n\n"
                "Возможные причины: не включена ни одна стратегия, "
                "не хватает рыночных данных, либо ROI ниже порога."
            )
            return
        cards = []
        for row in rows:
            gift = session.get(Gift, row.gift_id)
            cards.append(
                (
                    row.id,
                    gifts_service.describe(gift) if gift else "?",
                    Decimal(row.price_stars),
                    Decimal(row.fair_value_stars),
                    Decimal(row.net_roi),
                    row.risk_score,
                    row.confidence,
                    str(row.market),
                )
            )

    for cid, name, price, fair, roi, risk, conf, market in cards:
        conf_value = conf.value
        await message.answer(
            f"<b>{name}</b>\n"
            f"Площадка: {market}\n"
            f"Цена: <b>{gifts_service.format_stars(price)}</b> Stars\n"
            f"Оценка рынка: {gifts_service.format_stars(fair)} Stars\n"
            f"Чистый ROI: <b>{float(roi) * 100:.1f}%</b>\n"
            f"Риск: {risk}/100 · данные: {conf_value}",
            reply_markup=kb.candidate_actions(cid),
        )


@dp.callback_query(F.data.startswith("why:"))
async def cb_why(call: CallbackQuery) -> None:
    """Показать полное обоснование решения."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    cid = int(call.data.split(":")[1])
    with session_scope() as session:
        candidate = session.get(Candidate, cid)
        rationale = dict(candidate.rationale or {}) if candidate else {}

    reasons = rationale.get("reasons") or []
    market = rationale.get("market") or {}
    text = [
        "<b>Как посчитано</b>",
        f"Цена покупки: {rationale.get('buy_price', '—')} Stars",
        f"Себестоимость с комиссией: {rationale.get('total_cost', '—')}",
        f"Ожидаемая цена продажи: {rationale.get('expected_sale_price', '—')}",
        f"На руки после комиссий: {rationale.get('net_proceeds', '—')}",
        f"Чистая прибыль: <b>{rationale.get('net_profit', '—')}</b> Stars",
        "",
        f"Выборка: {market.get('sample_size', '—')} сделок, "
        f"источник {market.get('source', '—')}",
        f"Активных лотов: {market.get('active_listings', '—')}",
        f"Срок продажи: {market.get('days_to_sell', '—')} дн.",
        "",
        "<b>Факторы риска</b>",
    ]
    text.extend(f"• {r}" for r in reasons[:12])
    await call.message.answer("\n".join(text))
    await call.answer()


@dp.callback_query(F.data.startswith("skip:"))
async def cb_skip(call: CallbackQuery) -> None:
    """Отклонить кандидата."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    cid = int(call.data.split(":")[1])
    with session_scope() as session:
        candidate = session.get(Candidate, cid)
        if candidate is not None:
            candidate.state = "rejected"
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Пропущено")


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(call: CallbackQuery) -> None:
    """Первый шаг покупки: запросить подтверждение."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    cid = int(call.data.split(":")[1])
    with session_scope() as session:
        candidate = session.get(Candidate, cid)
        if candidate is None or candidate.state != "pending":
            await call.answer("Кандидат уже обработан", show_alert=True)
            return
        price = Decimal(candidate.price_stars)
        gift = session.get(Gift, candidate.gift_id)
        name = gifts_service.describe(gift) if gift else "?"

    await call.message.answer(
        f"⚠️ <b>Подтверждение покупки</b>\n\n"
        f"{name}\n"
        f"Списание: <b>{gifts_service.format_stars(price)} Stars</b>\n\n"
        "Операция необратима. Перед покупкой бот заново проверит цену "
        "на площадке и откажется, если она изменилась.",
        reply_markup=kb.confirm_buy(cid),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("confirmbuy:"))
async def cb_confirm_buy(call: CallbackQuery) -> None:
    """Реальная покупка после подтверждения владельцем."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    cid = int(call.data.split(":")[1])
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Выполняю…")

    result = await executor.execute_buy(
        cid, actor=str(call.from_user.id), mode=TradeMode.SEMI
    )

    if result.get("ok") is True:
        await call.message.answer(f"✅ {result['detail']}")
    elif result.get("ok") is None:
        await call.message.answer(
            f"⚠️ {result['detail']}\n\n"
            "Повторная покупка не выполняется. Результат появится после сверки."
        )
    else:
        await call.message.answer(f"❌ Покупка не выполнена: {result.get('detail')}")


@dp.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery) -> None:
    """Отмена действия."""
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Отменено")


# ----------------------------------------------------------------------
# Портфель и PnL
# ----------------------------------------------------------------------
@dp.message(F.text == "💼 Портфель")
@dp.message(Command("portfolio"))
async def cmd_portfolio(message: Message) -> None:
    """Открытые позиции."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    with session_scope() as session:
        positions = portfolio.open_positions(session)
        if not positions:
            await message.answer("Портфель пуст.")
            return
        lines = ["<b>Открытые позиции</b>", ""]
        for position in positions[:20]:
            gift = session.get(Gift, position.gift_id)
            status = position.status.value
            lines.append(
                f"#{position.id} {gifts_service.describe(gift) if gift else '?'}\n"
                f"   куплено: {gifts_service.format_stars(position.buy_price)} Stars · "
                f"{status}"
                + (
                    f" · в продаже за {gifts_service.format_stars(position.list_price)}"
                    if position.list_price
                    else ""
                )
            )
    await message.answer("\n".join(lines))


@dp.message(F.text == "📊 PnL")
@dp.message(Command("pnl"))
async def cmd_pnl(message: Message) -> None:
    """Финансовая сводка."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    with session_scope() as session:
        summary = portfolio.pnl_summary(session)
    await message.answer(
        "<b>Финансовый результат</b>\n\n"
        f"Закрытых сделок: {summary['closed_count']}\n"
        f"Открытых позиций: {summary['open_count']}\n"
        f"Реализованный PnL: <b>{gifts_service.format_stars(summary['realized_pnl'])}</b> Stars\n"
        f"ROI по закрытым: {float(summary['roi']) * 100:.1f}%\n"
        f"Доля прибыльных: {summary['win_rate'] * 100:.0f}%\n"
        f"Средний срок удержания: {summary['avg_hold_days']:.1f} дн.\n"
        f"Заморожено в позициях: {gifts_service.format_stars(summary['locked_cost'])} Stars"
    )


@dp.message(F.text == "💰 Баланс")
@dp.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    """Балансы аккаунтов, кошельков площадок и состояние бюджетов."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return

    from app.services import accounts as accounts_service
    from app.services import balances
    from app.services.gifts import format_amount

    await message.answer("Опрашиваю балансы…")

    # Свежие значения, а не то, что сохранилось с прошлого раза.
    try:
        await accounts_service.refresh_balances()
        await balances.refresh()
    except Exception as exc:  # noqa: BLE001 - показать хоть что-то
        log.warning("Опрос балансов не удался: %s", exc)

    lines = ["<b>Торговые аккаунты</b>"]
    with session_scope() as session:
        rows = accounts_service.all_accounts(session)
        if not rows:
            lines.append("— аккаунтов нет, добавьте в панели")
        for account in rows:
            state = "" if accounts_service.is_authorized(account) else " (вход не выполнен)"
            lines.append(
                f"<b>{account.name}</b>{state}\n"
                f"   Stars: {gifts_service.format_stars(account.stars_balance)} ★\n"
                f"   GRAM:  {format_amount(account.ton_balance)}"
            )

    lines.append("")
    lines.append("<b>Кошельки площадок</b>")
    market_rows = balances.snapshot()
    if not market_rows:
        lines.append("— ни одна площадка не подключена")
    for item in market_rows:
        if item["amount"] is not None:
            lines.append(
                f"<b>{item['title']}</b>: {format_amount(item['amount'])} "
                f"{display_currency(item['currency'])}"
            )
        else:
            reason = item.get("error") or "не опрашивался"
            lines.append(f"<b>{item['title']}</b>: {reason}")

    with session_scope() as session:
        lines.append("")
        lines.append("<b>Бюджеты стратегий</b>")
        budgets = session.query(Budget).all()
        if not budgets:
            lines.append("— бюджетов нет")
        for budget in budgets:
            snap = budget_service.snapshot(session, budget.id)
            currency = display_currency(snap["currency"])
            lines.append(
                f"{snap['name']}: потолок {format_amount(snap['hard_cap'])} {currency}, "
                f"свободно {format_amount(snap['available'])}, "
                f"в резерве {format_amount(snap['reserved'])}"
            )

    await message.answer("\n".join(lines))


# ----------------------------------------------------------------------
# Стратегии
# ----------------------------------------------------------------------
@dp.message(F.text == "⚙️ Стратегии")
@dp.message(Command("strategies"))
async def cmd_strategies(message: Message) -> None:
    """Список стратегий с управлением."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    with session_scope() as session:
        strategies = session.query(Strategy).order_by(Strategy.priority.desc()).all()
        if not strategies:
            await message.answer("Стратегий нет. Создайте: /newstrategy имя")
            return
        cards = []
        for item in strategies:
            budget = session.get(Budget, item.budget_id) if item.budget_id else None
            mode = item.mode.value
            cards.append(
                (
                    item.id,
                    item.name,
                    item.is_enabled,
                    mode,
                    Decimal(item.min_roi),
                    item.max_risk,
                    Decimal(budget.hard_cap) if budget else Decimal(0),
                    Decimal(budget.available) if budget else Decimal(0),
                    list(item.markets or []),
                )
            )

    for sid, name, enabled, mode, min_roi, max_risk, cap, avail, markets in cards:
        idle = strategy_service.idle_markets(markets)
        unused = (
            f"Не обходятся: {', '.join(idle)} — подключены, но не "
            f"выбраны в стратегии\n"
            if idle
            else ""
        )
        await message.answer(
            f"<b>{name}</b> — {'▶️ включена' if enabled else '⏸ выключена'}\n"
            f"Режим: {mode.upper()}\n"
            f"Площадки: {', '.join(markets) or '—'}\n"
            f"{unused}"
            f"Мин. ROI: {float(min_roi) * 100:.0f}% · макс. риск: {max_risk}\n"
            f"Бюджет: {gifts_service.format_stars(cap)} Stars "
            f"(свободно {gifts_service.format_stars(avail)})",
            reply_markup=kb.strategy_actions(sid, enabled),
        )


@dp.message(Command("newstrategy"))
async def cmd_new_strategy(message: Message) -> None:
    """Создать стратегию: /newstrategy имя"""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажите имя: <code>/newstrategy моя-стратегия</code>")
        return
    name = parts[1].strip()[:64]
    with session_scope() as session:
        strategy_service.create_strategy(session, name=name)
    await message.answer(
        f"Стратегия <b>{name}</b> создана (выключена, режим SAFE).\n"
        "Задайте бюджет и включите её в разделе «Стратегии»."
    )


@dp.callback_query(F.data.startswith("strtoggle:"))
async def cb_toggle_strategy(call: CallbackQuery) -> None:
    """Включить или выключить стратегию."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    sid = int(call.data.split(":")[1])
    with session_scope() as session:
        strategy = session.get(Strategy, sid)
        if strategy is None:
            await call.answer("Не найдена", show_alert=True)
            return
        budget = session.get(Budget, strategy.budget_id) if strategy.budget_id else None
        if not strategy.is_enabled and (budget is None or Decimal(budget.hard_cap) <= 0):
            await call.answer(
                "Сначала задайте бюджет — иначе покупать не на что", show_alert=True
            )
            return
        strategy.is_enabled = not strategy.is_enabled
        state = strategy.is_enabled
        name = strategy.name
    await call.answer("Включена" if state else "Выключена")
    await call.message.answer(
        f"Стратегия <b>{name}</b>: {'▶️ включена' if state else '⏸ выключена'}"
    )


@dp.callback_query(F.data.startswith("strbudget:"))
async def cb_strategy_budget(call: CallbackQuery) -> None:
    """Запросить новый потолок бюджета."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    sid = int(call.data.split(":")[1])
    _pending_input[call.from_user.id] = ("budget", sid)
    await call.message.answer(
        "Введите потолок бюджета стратегии в Stars (число).\n"
        "Это <b>жёсткий предел</b>: больше этой суммы стратегия не потратит."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("strroi:"))
async def cb_strategy_roi(call: CallbackQuery) -> None:
    """Запросить новый минимальный ROI."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    sid = int(call.data.split(":")[1])
    with session_scope() as session:
        strategy = session.get(Strategy, sid)
        if strategy is None:
            await call.answer("Не найдена", show_alert=True)
            return
        current = Decimal(strategy.min_roi or 0) * 100
        name = strategy.name

    _pending_input[call.from_user.id] = ("roi", sid)
    await call.message.answer(
        f"<b>{name}</b>: минимальный ROI сейчас <b>{current:.0f}%</b>\n\n"
        "Пришлите новое значение в процентах — например <code>20</code>.\n\n"
        "Это чистая прибыль после комиссий. На Portals комиссия около "
        "2,5%, на Telegram — около 20%, поэтому для одного и того же "
        "ROI на Telegram нужна заметно бо́льшая скидка от рынка."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("strmode:"))
async def cb_strategy_mode(call: CallbackQuery) -> None:
    """Выбор режима стратегии."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    sid = int(call.data.split(":")[1])
    await call.message.answer(
        "Выберите режим:\n\n"
        "<b>SAFE</b> — только рекомендации\n"
        "<b>SEMI</b> — покупка после подтверждения\n"
        "<b>AUTO</b> — автономно, только Telegram из белого списка",
        reply_markup=kb.mode_choice(sid),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("setmode:"))
async def cb_set_mode(call: CallbackQuery) -> None:
    """Применить режим стратегии."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    _, sid_raw, mode_raw = call.data.split(":")
    with session_scope() as session:
        strategy = session.get(Strategy, int(sid_raw))
        if strategy is None:
            await call.answer("Не найдена", show_alert=True)
            return
        strategy.mode = TradeMode(mode_raw)
        name = strategy.name
    await call.answer(f"Режим: {mode_raw.upper()}")
    await call.message.answer(f"Стратегия <b>{name}</b>: режим {mode_raw.upper()}")


# ----------------------------------------------------------------------
# Площадки и настройки
# ----------------------------------------------------------------------
@dp.message(F.text == "🔌 Площадки")
@dp.message(Command("markets"))
async def cmd_markets(message: Message) -> None:
    """Матрица возможностей площадок."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    icons = {"supported": "🟢", "experimental": "🟡", "unavailable": "⚪️"}
    matrix = capability_matrix()
    lines = ["<b>Возможности площадок</b>", ""]
    for market, caps in matrix.items():
        active = [
            f"{icons.get(status, '?')}{cap}"
            for cap, status in caps.items()
            if status != "unavailable"
        ]
        lines.append(f"<b>{market}</b>: {' '.join(active) if active else '— нет доступа'}")
    lines += [
        "",
        "🟢 официальный API — разрешён AUTO",
        "🟡 приватный API без SLA — только чтение и ручной режим",
        "⚪️ недоступно",
    ]
    await message.answer("\n".join(lines))


@dp.message(F.text == "🛠 Настройки")
@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Текущая конфигурация предохранителей."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    mode_value = runtime.mode().value
    session_state = (
        "авторизована" if settings.session_path.exists() else "НЕ авторизована"
    )
    await message.answer(
        "<b>Настройки</b>\n\n"
        f"Режим: <b>{mode_value.upper()}</b>\n"
        f"Аварийный стоп: {'ВКЛЮЧЁН' if runtime.kill_switch() else 'выключен'}\n"
        f"Суточный лимит: {settings.daily_limit_stars or '—'} Stars\n"
        f"Макс. позиций: {settings.max_open_positions}\n"
        f"Мин. ROI: {settings.min_roi * 100:.0f}%\n"
        f"Боевой режим включён: "
        f"{', '.join(sorted(runtime.auto_markets())) or '— нигде'}\n\n"
        f"MTProto-сессия: <b>{session_state}</b>\n"
        f"api_id задан: {'да' if settings.tg_api_id else 'НЕТ'}\n\n"
        "Режим, лимиты и боевой режим площадок меняются в веб-панели, "
        "раздел «Торговля» — без перезапуска."
    )


@dp.message(Command("scan"))
async def cmd_scan(message: Message) -> None:
    """Запустить сканирование вручную."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    await message.answer("Сканирую рынки…")
    from app.services.scanner import scan_once

    try:
        report = await scan_once()
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка скана: {type(exc).__name__}: {exc}")
        return
    await message.answer(
        f"Просмотрено лотов: {report['listings']}\n"
        f"Новых фактов продаж: {report['facts']}\n"
        f"Найдено кандидатов: <b>{report['candidates']}</b>"
    )


@dp.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    """Сверить портфель с инвентарём Telegram."""
    if not is_owner(message.from_user.id if message.from_user else None):
        await deny(message)
        return
    report = await portfolio.sync_inventory(Market.TELEGRAM)
    if not report.get("ok"):
        await message.answer(f"Сверка не удалась: {report.get('detail')}")
        return
    await message.answer(
        f"В инвентаре: {report['in_inventory']}\n"
        f"Отмечено проданными: {report['sold']}\n"
        f"Взято под управление: {report['adopted']}"
    )


# ----------------------------------------------------------------------
# Ввод чисел
# ----------------------------------------------------------------------
@dp.message(F.text.regexp(r"^\d+([.,]\d+)?$"))
async def on_number(message: Message) -> None:
    """Обработать введённое число для ожидающего действия."""
    user_id = message.from_user.id if message.from_user else None
    if not is_owner(user_id):
        await deny(message)
        return
    pending = _pending_input.pop(user_id, None)
    if pending is None:
        return
    action, target_id = pending
    value = Decimal((message.text or "0").replace(",", "."))

    if action == "budget":
        with session_scope() as session:
            strategy = session.get(Strategy, target_id)
            if strategy is None or not strategy.budget_id:
                await message.answer("Стратегия не найдена")
                return
            budget = session.get(Budget, strategy.budget_id)
            budget.hard_cap = value
            name = strategy.name
        await message.answer(
            f"Бюджет стратегии <b>{name}</b>: "
            f"{gifts_service.format_stars(value)} Stars.\n"
            "Больше этой суммы стратегия не потратит."
        )
    elif action == "roi":
        if value <= 0 or value >= 100:
            await message.answer(
                "ROI задаётся в процентах: осмысленный диапазон 5–80."
            )
            return
        with session_scope() as session:
            strategy = session.get(Strategy, target_id)
            if strategy is None:
                await message.answer("Стратегия не найдена")
                return
            strategy.min_roi = value / 100
            name = strategy.name
            session.add(
                AuditLog(
                    actor=str(user_id),
                    action="strategy.min_roi",
                    target=name,
                    payload={"min_roi": str(strategy.min_roi)},
                )
            )
        await message.answer(
            f"Стратегия <b>{name}</b>: минимальный ROI теперь "
            f"<b>{value:.0f}%</b>.\n"
            "Применится со следующего прохода сканера."
        )
    elif action == "listprice":
        result = await executor.execute_list(
            target_id, value, actor=str(user_id), mode=TradeMode.SEMI
        )
        await message.answer(
            ("✅ " if result.get("ok") else "❌ ") + str(result.get("detail"))
        )


@dp.callback_query(F.data.startswith("list:"))
async def cb_list(call: CallbackQuery) -> None:
    """Запросить цену выставления позиции."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    pid = int(call.data.split(":")[1])
    _pending_input[call.from_user.id] = ("listprice", pid)
    await call.message.answer("Введите цену выставления в Stars (число).")
    await call.answer()


@dp.callback_query(F.data.startswith("unlist:"))
async def cb_unlist(call: CallbackQuery) -> None:
    """Снять позицию с продажи."""
    if not is_owner(call.from_user.id if call.from_user else None):
        await deny(call)
        return
    pid = int(call.data.split(":")[1])
    result = await executor.execute_cancel(pid, actor=str(call.from_user.id))
    await call.answer(str(result.get("detail"))[:180], show_alert=True)


# ----------------------------------------------------------------------
async def main() -> None:
    """Запустить бота."""
    from app.adapters import telegram_gateway

    setup_logging("bot")
    # Сессией владеет воркер — бот работает с копией ключа.
    telegram_gateway.prefer_detached()
    token = secrets.resolve("BOT_TOKEN", settings.bot_token)
    if not token:
        raise SystemExit(
            "BOT_TOKEN не задан. Получите токен у @BotFather и укажите "
            "в веб-панели (Настройки) либо в файле .env"
        )
    if not owner_ids():
        log.warning(
            "OWNER_IDS пуст — бот не будет отвечать никому. "
            "Узнайте свой id командой /id и впишите его в .env"
        )

    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
