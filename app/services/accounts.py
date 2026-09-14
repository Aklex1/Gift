"""Управление торговыми Telegram-аккаунтами.

Несколько аккаунтов нужны по двум причинам:

* FloodWait Telegram считается по аккаунту — распределяя поиск,
  система сканирует чаще, не приближаясь к порогу ни на одном;
* средства и подарки лежат раздельно, поэтому проблема с одним
  аккаунтом не останавливает торговлю целиком.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from decimal import Decimal
from itertools import cycle
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import decrypt, encrypt
from app.models import Account, utcnow

log = logging.getLogger(__name__)

#: Сколько ждать ответа от аккаунта при опросе баланса.
#: Без ограничения недоступный аккаунт подвешивал бы и панель, и CLI:
#: MTProto не HTTP, соединение может висеть до системного таймаута.
BALANCE_TIMEOUT = 20.0

#: Имя сессии становится именем файла — допускаем только безопасные символы.
_SAFE_NAME = re.compile(r"[^a-z0-9_-]+")


class AccountError(Exception):
    """Ошибка работы с аккаунтом."""


def session_name_for(name: str) -> str:
    """Построить безопасное имя файла сессии из имени аккаунта."""
    slug = _SAFE_NAME.sub("-", name.strip().lower()).strip("-")
    return slug or "account"


def session_path(account: Account) -> Path:
    """Путь к файлу сессии аккаунта."""
    return settings.data_dir / f"{account.session_name}.session"


def is_authorized(account: Account) -> bool:
    """Действительно ли аккаунт авторизован.

    Наличия файла сессии недостаточно: Telethon создаёт его при первом
    же подключении, ещё до ввода кода. Признак настоящего входа —
    сохранённый ключ авторизации внутри файла.
    """
    path = session_path(account)
    if not path.exists():
        return False
    try:
        import sqlite3

        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            row = con.execute(
                "SELECT auth_key FROM sessions LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        # Файл ещё пустой или повреждён — считаем, что входа не было.
        return False
    return bool(row and row[0])


def api_hash_of(account: Account) -> str:
    """Расшифрованный api_hash аккаунта."""
    try:
        return decrypt(account.api_hash_enc) or ""
    except RuntimeError as exc:
        log.error("Аккаунт %s: api_hash не расшифрован (%s)", account.name, exc)
        return ""


def create(
    session: Session,
    *,
    name: str,
    api_id: int,
    api_hash: str,
    phone: str | None = None,
    ton_address: str | None = None,
) -> Account:
    """Завести новый аккаунт.

    Raises:
        AccountError: имя занято или данные неполные.
    """
    name = name.strip()
    if not name:
        raise AccountError("Не указано имя аккаунта")
    if not api_id or not api_hash.strip():
        raise AccountError("Нужны api_id и api_hash с my.telegram.org")
    if session.query(Account).filter_by(name=name).first():
        raise AccountError(f"Аккаунт с именем {name!r} уже есть")

    slug = session_name_for(name)
    if session.query(Account).filter_by(session_name=slug).first():
        slug = f"{slug}-{int(utcnow().timestamp())}"

    account = Account(
        name=name,
        api_id=int(api_id),
        api_hash_enc=encrypt(api_hash.strip()) or "",
        phone=(phone or "").strip() or None,
        session_name=slug,
        ton_address=(ton_address or "").strip() or None,
        is_active=True,
        can_trade=True,
    )
    session.add(account)
    session.flush()
    log.info("Добавлен аккаунт %r (сессия %s)", name, slug)
    return account


def delete(session: Session, account_id: int, *, drop_session_file: bool = True) -> None:
    """Удалить аккаунт и, по желанию, файл его сессии."""
    account = session.get(Account, account_id)
    if account is None:
        return
    path = session_path(account)
    name = account.name
    session.delete(account)
    session.flush()

    if drop_session_file:
        for candidate in (path, path.with_suffix(".session-journal")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError as exc:  # noqa: PERF203 - удаление не критично
                log.warning("Не удалось удалить %s: %s", candidate, exc)
    log.info("Аккаунт %r удалён", name)


def all_accounts(session: Session) -> list[Account]:
    """Все аккаунты в порядке добавления."""
    return session.query(Account).order_by(Account.id.asc()).all()


def usable(session: Session, *, for_trade: bool = False) -> list[Account]:
    """Аккаунты, пригодные для работы прямо сейчас.

    Исключаются выключенные, неавторизованные и находящиеся под
    FloodWait.
    """
    now = utcnow()
    out: list[Account] = []
    for account in session.query(Account).filter_by(is_active=True).all():
        if for_trade and not account.can_trade:
            continue
        if account.flood_until and account.flood_until > now:
            continue
        if not is_authorized(account):
            continue
        out.append(account)
    return out


def pick_for_trade(
    session: Session, *, strategy_account_id: int | None = None, amount: Decimal | None = None
) -> Account | None:
    """Выбрать аккаунт для покупки.

    Приоритет: аккаунт, закреплённый за стратегией. Иначе — тот, у
    кого хватает Stars; при прочих равных с наибольшим балансом,
    чтобы равномернее расходовать средства.
    """
    if strategy_account_id:
        account = session.get(Account, strategy_account_id)
        if account and account.is_active and account.can_trade:
            if not (account.flood_until and account.flood_until > utcnow()):
                return account
        log.warning(
            "Аккаунт стратегии id=%s недоступен, сделка не выполняется",
            strategy_account_id,
        )
        return None

    candidates = usable(session, for_trade=True)
    if amount is not None:
        with_funds = [
            a for a in candidates
            if a.stars_balance is not None and Decimal(a.stars_balance) >= amount
        ]
        # Если балансы ещё не опрашивались, не отсекаем никого.
        if with_funds:
            candidates = with_funds

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda a: Decimal(a.stars_balance) if a.stars_balance is not None else Decimal(0),
    )


def round_robin(accounts: list[Account]):
    """Бесконечный перебор аккаунтов для распределения чтения."""
    return cycle(accounts) if accounts else iter(())


def mark_flood(session: Session, account_id: int, seconds: float) -> None:
    """Отметить, что аккаунт получил FloodWait."""
    account = session.get(Account, account_id)
    if account is None:
        return
    account.flood_until = utcnow() + dt.timedelta(seconds=seconds + 1)
    log.warning(
        "Аккаунт %s под FloodWait до %s", account.name, account.flood_until
    )


def record_error(session: Session, account_id: int, message: str) -> None:
    """Записать последнюю ошибку аккаунта для интерфейса."""
    account = session.get(Account, account_id)
    if account is not None:
        account.last_error = message[:1000]


def update_balances(
    session: Session,
    account_id: int,
    *,
    stars: Decimal | None = None,
    ton: Decimal | None = None,
) -> None:
    """Сохранить свежие балансы аккаунта."""
    account = session.get(Account, account_id)
    if account is None:
        return
    if stars is not None:
        account.stars_balance = stars
    if ton is not None:
        account.ton_balance = ton
    account.balance_at = utcnow()


def update_identity(
    session: Session, account_id: int, *, tg_user_id: int | None, username: str | None
) -> None:
    """Запомнить, под каким Telegram-аккаунтом авторизована сессия."""
    account = session.get(Account, account_id)
    if account is None:
        return
    account.tg_user_id = tg_user_id
    account.tg_username = username


def adopt_legacy(session: Session) -> Account | None:
    """Перенести единственный аккаунт из .env в таблицу.

    Нужно при обновлении: у работающей установки уже есть сессия,
    настроенная через TG_API_ID/TG_API_HASH, и терять её нельзя.
    """
    if session.query(Account).count():
        return None

    from app.services import secrets

    api_id_raw = secrets.resolve("TG_API_ID", str(settings.tg_api_id or ""))
    api_hash = secrets.resolve("TG_API_HASH", settings.tg_api_hash)
    if not api_id_raw or not api_hash:
        return None
    try:
        api_id = int(api_id_raw)
    except ValueError:
        return None

    account = Account(
        name="основной",
        api_id=api_id,
        api_hash_enc=encrypt(api_hash) or "",
        phone=secrets.resolve("TG_PHONE", settings.tg_phone) or None,
        # Сохраняем прежнее имя сессии, чтобы файл остался рабочим.
        session_name=settings.tg_session_name,
        ton_address=secrets.resolve(
            "TON_WALLET_ADDRESS", settings.ton_wallet_address
        )
        or None,
        is_active=True,
        can_trade=True,
    )
    session.add(account)
    session.flush()
    log.info("Существующая сессия перенесена в аккаунт %r", account.name)
    return account


async def refresh_balances(account_ids: list[int] | None = None) -> dict:
    """Опросить балансы Stars и TON по аккаунтам.

    Ошибка на одном аккаунте не прерывает опрос остальных: её видно
    в карточке аккаунта в панели.
    """
    from app.adapters.registry import telegram_adapter_for
    from app.adapters.ton import TonClient
    from app.db import session_scope
    from app.enums import Currency

    with session_scope() as session:
        rows = all_accounts(session)
        plan = [
            {
                "id": a.id,
                "name": a.name,
                "ton": a.ton_address,
                # Без выполненного входа в Telegram идти незачем.
                "authorized": is_authorized(a),
            }
            for a in rows
            if a.is_active and (account_ids is None or a.id in account_ids)
        ]

    report = {"checked": 0, "ok": 0, "failed": 0}
    ton_client = TonClient()

    try:
        for item in plan:
            report["checked"] += 1
            stars: Decimal | None = None
            ton: Decimal | None = None
            error = ""

            if not item["authorized"]:
                # Вход не выполнен — сразу сообщаем об этом, не ожидая сети.
                with session_scope() as session:
                    record_error(session, item["id"], "вход не выполнен")
                report["failed"] += 1
                continue

            with session_scope() as session:
                account = session.get(Account, item["id"])
                if account is None:
                    continue
                adapter = telegram_adapter_for(account)

            try:
                balances = await asyncio.wait_for(
                    adapter.balance(), timeout=BALANCE_TIMEOUT
                )
                for balance in balances:
                    if balance.currency is Currency.STARS:
                        stars = balance.amount
                    elif balance.currency is Currency.TON:
                        ton = balance.amount
            except asyncio.TimeoutError:
                error = f"аккаунт не ответил за {BALANCE_TIMEOUT:.0f} c"
                log.warning("Аккаунт %s: %s", item["name"], error)
            except Exception as exc:  # noqa: BLE001 - один аккаунт не ломает опрос
                error = f"{type(exc).__name__}: {exc}"
                log.warning("Аккаунт %s: баланс не получен: %s", item["name"], error)

            # Баланс кошелька TON читается отдельно: он вне Telegram.
            if item["ton"]:
                try:
                    ton = await asyncio.wait_for(
                        ton_client.balance(item["ton"]), timeout=BALANCE_TIMEOUT
                    )
                except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                    log.debug("Аккаунт %s: баланс TON недоступен: %s", item["name"], exc)

            with session_scope() as session:
                update_balances(session, item["id"], stars=stars, ton=ton)
                if error:
                    record_error(session, item["id"], error)
                    report["failed"] += 1
                else:
                    record_error(session, item["id"], "")
                    report["ok"] += 1
    finally:
        await ton_client.close()

    log.info("Балансы аккаунтов обновлены: %s", report)
    return report


async def verify_session(account_id: int) -> dict:
    """Проверить сессию аккаунта и запомнить, кому она принадлежит."""
    from app.adapters.registry import telegram_adapter_for
    from app.db import session_scope

    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            return {"ok": False, "detail": "аккаунт не найден"}
        if not is_authorized(account):
            return {
                "ok": False,
                "detail": (
                    f"вход не выполнен — на сервере: "
                    f"gift-cli login --account {account.name}"
                ),
            }
        adapter = telegram_adapter_for(account)

    try:
        me = await asyncio.wait_for(adapter.gateway.me(), timeout=BALANCE_TIMEOUT)
    except asyncio.TimeoutError:
        detail = f"аккаунт не ответил за {BALANCE_TIMEOUT:.0f} c"
        with session_scope() as session:
            record_error(session, account_id, detail)
        return {"ok": False, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        with session_scope() as session:
            record_error(session, account_id, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "detail": str(exc)}

    with session_scope() as session:
        update_identity(
            session,
            account_id,
            tg_user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
        )
        record_error(session, account_id, "")
    return {
        "ok": True,
        "detail": f"@{getattr(me, 'username', '—')} (id={getattr(me, 'id', '?')})",
    }
