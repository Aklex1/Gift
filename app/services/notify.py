"""Уведомления владельцу в Telegram.

Без них о проблеме можно узнать, только открыв панель: аккаунт под
FloodWait, исчерпан суточный лимит, позиция висит неделю, площадка
отвалилась. ТЗ относит это к блоку операций (метрики и алерты).

Два правила, чтобы уведомления не превратились в шум:

* каждое событие имеет ключ и паузу — повтор о той же проблеме не
  приходит, пока пауза не истекла;
* уведомление о том, что проблема ушла, отправляется один раз и
  сбрасывает паузу.

Отправка идёт напрямую через Bot API: воркер не должен зависеть от
того, запущен ли процесс бота.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx

from app.config import settings
from app.db import session_scope
from app.services import secrets, store

log = logging.getLogger(__name__)

TIMEOUT = 15.0

#: Пауза по умолчанию между повторами одного уведомления.
DEFAULT_COOLDOWN = dt.timedelta(hours=6)

#: Ключ настройки: какие уведомления отправлять.
KEY_ENABLED = "NOTIFY_ENABLED"

#: Все доступные виды уведомлений.
KINDS: dict[str, str] = {
    "limit": "исчерпан суточный лимит",
    "flood": "аккаунт под ограничением Telegram",
    "market": "площадка недоступна",
    "stale": "позиция долго не продаётся",
    "unknown": "неизвестный исход сделки не сведён",
    "scan": "сканер не работает",
    "balance": "заканчиваются средства",
    "trade": "совершена сделка",
    "token": "не удалось продлить токен площадки",
}

#: Виды, включённые по умолчанию.
DEFAULT_ENABLED = {
    "limit", "flood", "market", "stale", "unknown", "scan", "trade", "token",
}


def enabled_kinds() -> set[str]:
    """Какие уведомления сейчас включены."""
    raw = store.get(KEY_ENABLED)
    if raw is None:
        return set(DEFAULT_ENABLED)
    return {k.strip() for k in raw.split(",") if k.strip() in KINDS}


def set_enabled_kinds(kinds: set[str], *, actor: str = "web") -> None:
    """Задать список включённых уведомлений."""
    from app.services import runtime

    valid = sorted(k for k in kinds if k in KINDS)
    store.set(KEY_ENABLED, ",".join(valid))
    runtime._audit(actor, "notify.enabled", ", ".join(valid) or "выключены все")


def _owner_ids() -> list[int]:
    """Кому отправлять."""
    raw = secrets.resolve("OWNER_IDS", settings.owner_ids)
    out: list[int] = []
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.append(int(chunk))
    return out


async def send(text: str) -> bool:
    """Отправить сообщение владельцам.

    Returns:
        True, если хотя бы одному доставлено.
    """
    token = secrets.resolve("BOT_TOKEN", settings.bot_token)
    owners = _owner_ids()
    if not token or not owners:
        log.debug("Уведомления не настроены: нет токена или владельцев")
        return False

    delivered = False
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for chat_id in owners:
            try:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if response.status_code == 200:
                    delivered = True
                else:
                    log.warning(
                        "Уведомление не доставлено (%s): %s",
                        response.status_code,
                        response.text[:200],
                    )
            except Exception as exc:  # noqa: BLE001 - связь не должна ломать воркер
                log.warning("Уведомление не отправлено: %s", exc)
    return delivered


def _state_key(key: str) -> str:
    """Ключ хранения времени последней отправки."""
    return f"NOTIFY_LAST_{key}"


async def alert(
    kind: str,
    key: str,
    text: str,
    *,
    cooldown: dt.timedelta = DEFAULT_COOLDOWN,
) -> bool:
    """Сообщить о проблеме, не повторяясь.

    Args:
        kind: вид уведомления из ``KINDS``.
        key: чем именно вызвано — например, имя аккаунта. Пауза
            считается отдельно для каждого ключа.
        cooldown: как долго не повторять то же самое.
    """
    if kind not in enabled_kinds():
        return False

    full_key = f"{kind}:{key}"
    last = store.get(_state_key(full_key))
    now = dt.datetime.utcnow()
    if last:
        try:
            if now - dt.datetime.fromisoformat(last) < cooldown:
                return False
        except (ValueError, TypeError):
            pass

    if await send(text):
        store.set(_state_key(full_key), now.isoformat(timespec="seconds"))
        return True
    return False


async def resolved(kind: str, key: str, text: str) -> bool:
    """Сообщить, что проблема ушла, и снять паузу."""
    full_key = f"{kind}:{key}"
    if not store.get(_state_key(full_key)):
        # О проблеме не сообщали — значит и о снятии сообщать нечего.
        return False
    store.set(_state_key(full_key), "")
    if kind not in enabled_kinds():
        return False
    return await send(text)


# ----------------------------------------------------------------------
# Проверки состояния
# ----------------------------------------------------------------------
async def check_all() -> dict:
    """Проверить всё, о чём стоит предупредить."""
    report = {"sent": 0, "checked": 0}

    for check in (
        _check_daily_limits,
        _check_accounts,
        _check_markets,
        _check_stale_positions,
        _check_unknown_intents,
        _check_scanner,
    ):
        report["checked"] += 1
        try:
            if await check():
                report["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - проверка не должна ронять воркер
            log.warning("Проверка %s не выполнена: %s", check.__name__, exc)
    return report


async def _check_daily_limits() -> bool:
    """Суточный лимит исчерпан или почти исчерпан."""
    from app.services import limits, runtime

    sent = False
    with session_scope() as session:
        total = limits.remaining_today(session)
        limit = limits.daily_total_limit()
        if limit and total is not None and total <= 0:
            sent |= await alert(
                "limit",
                "total",
                f"🛑 <b>Суточный лимит исчерпан</b>\n\n"
                f"Потрачено {limits.spent_today(session):.0f} из {limit:.0f} Stars.\n"
                f"Покупки возобновятся после полуночи UTC.",
                cooldown=dt.timedelta(hours=12),
            )

        for market in runtime.TRADABLE:
            market_limit = limits.daily_market_limit(market)
            if not market_limit:
                continue
            left = limits.remaining_today(session, market)
            if left is not None and left <= 0:
                currency = runtime.cap_currency(market).value
                sent |= await alert(
                    "limit",
                    market.value,
                    f"🛑 <b>Суточный лимит {market.value} исчерпан</b>\n\n"
                    f"Потрачено {limits.spent_today(session, market):.2f} из "
                    f"{market_limit:.2f} {currency}.",
                    cooldown=dt.timedelta(hours=12),
                )
    return sent


async def _check_accounts() -> bool:
    """Аккаунт под FloodWait или с ошибкой."""
    from app.models import Account, utcnow
    from app.services import accounts as accounts_service

    sent = False
    with session_scope() as session:
        for account in session.query(Account).filter_by(is_active=True).all():
            if account.flood_until and account.flood_until > utcnow():
                minutes = int(
                    (account.flood_until - utcnow()).total_seconds() // 60
                )
                sent |= await alert(
                    "flood",
                    account.name,
                    f"⏳ <b>Аккаунт {account.name} на паузе</b>\n\n"
                    f"Telegram ограничил частоту запросов ещё на "
                    f"{minutes} мин. Поиск идёт через другие аккаунты, "
                    f"если они есть.",
                    cooldown=dt.timedelta(hours=2),
                )
            elif not accounts_service.is_authorized(account):
                sent |= await alert(
                    "flood",
                    f"auth:{account.name}",
                    f"🔑 <b>Аккаунт {account.name}: вход не выполнен</b>\n\n"
                    f"На сервере: <code>gift-cli login --account "
                    f"{account.name}</code>",
                    cooldown=dt.timedelta(hours=24),
                )
            else:
                await resolved(
                    "flood",
                    account.name,
                    f"✅ Аккаунт {account.name} снова работает.",
                )
    return sent


async def _check_markets() -> bool:
    """Площадка перестала отвечать."""
    from app.services import balances, runtime

    sent = False
    for item in balances.snapshot():
        if not item["enabled"]:
            continue
        if item["error"]:
            sent |= await alert(
                "market",
                item["market"],
                f"⚠️ <b>{item['title']} недоступна</b>\n\n{item['error']}\n\n"
                f"Торговля по этой площадке остановлена, остальные работают.",
                cooldown=dt.timedelta(hours=6),
            )
        elif item["amount"] is not None:
            await resolved(
                "market",
                item["market"],
                f"✅ {item['title']} снова отвечает. "
                f"Баланс: {item['amount']} {item['currency']}.",
            )
    return sent


async def _check_stale_positions(days: int = 7) -> bool:
    """Позиция долго не продаётся."""
    from app.enums import PositionStatus
    from app.models import Gift, Position, utcnow
    from app.services import gifts as gifts_service

    threshold = utcnow() - dt.timedelta(days=days)
    sent = False
    with session_scope() as session:
        rows = (
            session.query(Position)
            .filter(
                Position.status == PositionStatus.LISTED.value,
                Position.listed_at < threshold,
            )
            .all()
        )
        for position in rows:
            gift = session.get(Gift, position.gift_id)
            age = (utcnow() - position.listed_at).days if position.listed_at else days
            sent |= await alert(
                "stale",
                f"pos:{position.id}",
                f"🐌 <b>Позиция не продаётся {age} дн.</b>\n\n"
                f"{gifts_service.describe(gift) if gift else '?'}\n"
                f"Куплено за {position.buy_price}, выставлено за "
                f"{position.list_price}.\n\n"
                f"Цена уже у нижней границы или рынок ушёл. Можно снизить "
                f"минимальную маржу стратегии либо снять с продажи.",
                cooldown=dt.timedelta(days=3),
            )
    return sent


async def _check_unknown_intents() -> bool:
    """Неизвестный исход так и не сведён."""
    from app.enums import IntentStatus
    from app.models import Intent
    from app.services.reconciler import MAX_ATTEMPTS

    sent = False
    with session_scope() as session:
        rows = (
            session.query(Intent)
            .filter(
                Intent.status == IntentStatus.UNKNOWN.value,
                Intent.reconcile_attempts >= MAX_ATTEMPTS,
            )
            .all()
        )
        for intent in rows:
            sent |= await alert(
                "unknown",
                f"intent:{intent.id}",
                f"❓ <b>Исход сделки не выяснен</b>\n\n"
                f"Намерение #{intent.id}, {intent.market}, "
                f"лот {intent.listing_external_id}.\n"
                f"Попыток сверки: {intent.reconcile_attempts}.\n\n"
                f"Деньги остаются зарезервированными. Проверьте подарок "
                f"на площадке вручную.",
                cooldown=dt.timedelta(hours=12),
            )
    return sent


async def _check_scanner() -> bool:
    """Сканер не отрабатывает."""
    from app.services import scanner

    report = scanner.last_report()
    if not report:
        return False

    if report.get("error"):
        return await alert(
            "scan",
            "error",
            f"⚠️ <b>Сканер падает с ошибкой</b>\n\n{report['error']}\n\n"
            f"Находки не появляются, пока это не исправлено.",
            cooldown=dt.timedelta(hours=3),
        )

    # Один и тот же расчёт, что и в панели. Когда он был написан
    # дважды, версии разошлись: панель показывала исправный сканер, а
    # бот каждые три часа слал тревогу — он сравнивал возраст прохода с
    # интервалом запуска, хотя проход идёт дольше интервала.
    state = scanner.liveness(report)
    if state.stale:
        return await alert(
            "scan",
            "stale",
            f"⚠️ <b>Сканер молчит</b>\n\n"
            f"{state.detail}\n"
            f"Проверьте: <code>systemctl status gift-worker</code>",
            cooldown=dt.timedelta(hours=3),
        )

    await resolved("scan", "error", "✅ Сканер снова работает.")
    await resolved("scan", "stale", "✅ Сканер снова работает.")
    return False


async def notify_trade(
    *, ok: bool, market: str, name: str, price: Decimal, currency: str, detail: str = ""
) -> bool:
    """Сообщить о состоявшейся сделке.

    Без паузы: каждая сделка важна сама по себе.
    """
    if "trade" not in enabled_kinds():
        return False
    mark = "✅" if ok else "❌"
    title = "Покупка совершена" if ok else "Покупка не прошла"
    text = (
        f"{mark} <b>{title}</b>\n\n"
        f"{name}\n{market} · {price} {currency}"
    )
    if detail:
        text += f"\n\n{detail}"
    return await send(text)
