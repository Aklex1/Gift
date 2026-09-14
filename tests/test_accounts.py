"""Тесты многоаккаунтности и суточных лимитов."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Currency, Market
from app.services import accounts as accounts_service


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Изолированный каталог для файлов сессий."""
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def _make(session, name="основной", **kw):
    """Создать аккаунт с типовыми данными."""
    return accounts_service.create(
        session, name=name, api_id=kw.pop("api_id", 12345),
        api_hash=kw.pop("api_hash", "abcdef0123456789abcdef0123456789"), **kw
    )


def _authorize(account, data_dir):
    """Сымитировать успешный вход: в сессии появляется ключ авторизации."""
    import sqlite3

    con = sqlite3.connect(accounts_service.session_path(account))
    con.execute("CREATE TABLE IF NOT EXISTS sessions (dc_id integer, auth_key blob)")
    con.execute("INSERT INTO sessions VALUES (2, ?)", (b"k" * 256,))
    con.commit()
    con.close()


# ----------------------------------------------------------------------
# Хранение
# ----------------------------------------------------------------------
def test_api_hash_is_encrypted(session, data_dir):
    """api_hash не лежит в базе открытым текстом."""
    account = _make(session)
    assert "abcdef0123456789" not in account.api_hash_enc
    assert account.api_hash_enc.startswith("enc::")
    assert accounts_service.api_hash_of(account) == "abcdef0123456789abcdef0123456789"


def test_each_account_gets_own_session_file(session, data_dir):
    """У аккаунтов разные файлы сессий."""
    first = _make(session, name="первый")
    second = _make(session, name="второй")
    assert accounts_service.session_path(first) != accounts_service.session_path(second)


def test_duplicate_name_rejected(session, data_dir):
    """Имя аккаунта уникально."""
    _make(session, name="основной")
    with pytest.raises(accounts_service.AccountError):
        _make(session, name="основной")


def test_session_name_is_filesystem_safe(session, data_dir):
    """Имя файла сессии не содержит опасных символов."""
    account = _make(session, name="Мой аккаунт / тест #1")
    assert "/" not in account.session_name
    assert " " not in account.session_name


# ----------------------------------------------------------------------
# Выбор аккаунта
# ----------------------------------------------------------------------
def test_unauthorized_account_is_not_usable(session, data_dir):
    """Без файла сессии аккаунт в работу не берётся."""
    _make(session)
    assert accounts_service.usable(session) == []


def test_authorized_account_is_usable(session, data_dir):
    """После входа аккаунт доступен."""
    account = _make(session)
    _authorize(account, data_dir)
    assert [a.id for a in accounts_service.usable(session)] == [account.id]


def test_flood_wait_excludes_account(session, data_dir):
    """Аккаунт под FloodWait не опрашивается."""
    account = _make(session)
    _authorize(account, data_dir)
    accounts_service.mark_flood(session, account.id, 300)
    assert accounts_service.usable(session) == []


def test_disabled_account_excluded(session, data_dir):
    """Выключенный аккаунт не используется."""
    account = _make(session)
    _authorize(account, data_dir)
    account.is_active = False
    session.flush()
    assert accounts_service.usable(session) == []


def test_read_only_account_excluded_from_trading(session, data_dir):
    """Аккаунт без права торговли годится только для поиска."""
    account = _make(session)
    _authorize(account, data_dir)
    account.can_trade = False
    session.flush()

    assert len(accounts_service.usable(session)) == 1
    assert accounts_service.usable(session, for_trade=True) == []


def test_pick_prefers_account_with_funds(session, data_dir):
    """Для покупки выбирается аккаунт, которому хватает Stars."""
    poor = _make(session, name="бедный")
    rich = _make(session, name="богатый")
    _authorize(poor, data_dir)
    _authorize(rich, data_dir)
    accounts_service.update_balances(session, poor.id, stars=Decimal("100"))
    accounts_service.update_balances(session, rich.id, stars=Decimal("5000"))

    picked = accounts_service.pick_for_trade(session, amount=Decimal("1000"))
    assert picked is not None and picked.name == "богатый"


def test_strategy_account_is_respected(session, data_dir):
    """Стратегия, привязанная к аккаунту, торгует только с него."""
    first = _make(session, name="первый")
    second = _make(session, name="второй")
    _authorize(first, data_dir)
    _authorize(second, data_dir)

    picked = accounts_service.pick_for_trade(
        session, strategy_account_id=second.id, amount=Decimal("10")
    )
    assert picked is not None and picked.id == second.id


def test_strategy_account_unavailable_blocks_trade(session, data_dir):
    """Если закреплённый аккаунт недоступен, сделка не уходит на чужой."""
    first = _make(session, name="первый")
    second = _make(session, name="второй")
    _authorize(first, data_dir)
    _authorize(second, data_dir)
    accounts_service.mark_flood(session, second.id, 300)

    assert (
        accounts_service.pick_for_trade(session, strategy_account_id=second.id) is None
    )


# ----------------------------------------------------------------------
# Суточные лимиты
# ----------------------------------------------------------------------
@pytest.fixture()
def clean_store(monkeypatch, session):
    """Изолировать хранилище настроек."""
    from contextlib import contextmanager

    from app.services import store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr("app.services.runtime._audit", lambda *a, **k: None)
    store.invalidate()
    yield
    store.invalidate()


def _buy(session, market: Market, amount: str, currency=Currency.STARS, days_ago=0):
    """Записать покупку в журнал транзакций."""
    from app.models import Transaction

    session.add(
        Transaction(
            market=market,
            kind="buy",
            amount=Decimal(amount),
            currency=currency,
            happened_at=dt.datetime.utcnow() - dt.timedelta(days=days_ago),
        )
    )
    session.flush()


def test_no_limit_means_no_block(session, clean_store):
    """Без заданного лимита проверка ничего не запрещает."""
    from app.services import limits

    assert limits.check(
        session, market=Market.TELEGRAM,
        amount=Decimal("9999"), amount_stars=Decimal("9999"),
    ) == ""


def test_total_daily_limit_blocks(session, clean_store):
    """Общий суточный лимит останавливает торговлю."""
    from app.services import limits

    limits.set_daily_total_limit(Decimal("1000"))
    _buy(session, Market.TELEGRAM, "800")

    reason = limits.check(
        session, market=Market.TELEGRAM,
        amount=Decimal("300"), amount_stars=Decimal("300"),
    )
    assert "общий суточный лимит" in reason


def test_market_daily_limit_blocks(session, clean_store):
    """Лимит площадки действует независимо от общего."""
    from app.services import limits

    limits.set_daily_market_limit(Market.PORTALS, Decimal("5"))
    _buy(session, Market.PORTALS, "4", currency=Currency.TON)

    reason = limits.check(
        session, market=Market.PORTALS,
        amount=Decimal("2"), amount_stars=Decimal("800"),
    )
    assert "суточный лимит portals" in reason


def test_limits_reset_next_day(session, clean_store):
    """Вчерашние траты не мешают сегодняшним."""
    from app.services import limits

    limits.set_daily_total_limit(Decimal("1000"))
    _buy(session, Market.TELEGRAM, "900", days_ago=1)

    assert limits.spent_today(session) == Decimal("0")
    assert limits.check(
        session, market=Market.TELEGRAM,
        amount=Decimal("500"), amount_stars=Decimal("500"),
    ) == ""


def test_market_limit_counts_only_its_market(session, clean_store):
    """Траты одной площадки не съедают лимит другой."""
    from app.services import limits

    limits.set_daily_market_limit(Market.PORTALS, Decimal("5"))
    _buy(session, Market.MRKT, "4", currency=Currency.TON)

    assert limits.spent_today(session, Market.PORTALS) == Decimal("0")
    assert limits.check(
        session, market=Market.PORTALS,
        amount=Decimal("3"), amount_stars=Decimal("1200"),
    ) == ""


def test_remaining_shown_for_ui(session, clean_store):
    """Остаток лимита считается для интерфейса."""
    from app.services import limits

    limits.set_daily_total_limit(Decimal("1000"))
    _buy(session, Market.TELEGRAM, "250")
    assert limits.remaining_today(session) == Decimal("750")


def test_empty_session_file_is_not_authorized(session, data_dir):
    """Пустой файл сессии не означает выполненный вход.

    Telethon создаёт файл при первом подключении, ещё до ввода кода.
    Принимать его за авторизацию — значит слать запросы и ждать
    таймаута вместо понятного «войдите».
    """
    account = _make(session)
    accounts_service.session_path(account).write_text("", encoding="utf-8")
    assert accounts_service.is_authorized(account) is False
    assert accounts_service.usable(session) == []


def test_session_with_auth_key_is_authorized(session, data_dir):
    """Сессия с сохранённым ключом считается авторизованной."""
    import sqlite3

    account = _make(session)
    path = accounts_service.session_path(account)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions (dc_id integer, auth_key blob)")
    con.execute("INSERT INTO sessions VALUES (2, ?)", (b"x" * 256,))
    con.commit()
    con.close()

    assert accounts_service.is_authorized(account) is True
    assert len(accounts_service.usable(session)) == 1
