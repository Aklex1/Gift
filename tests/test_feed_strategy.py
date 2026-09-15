"""Тесты стратегии, ведомой каналом находок.

Главное: стратегия канала и обычные стратегии не работают вместе, а
потолок цены никогда не выходит за лимит площадки — канал показывает
покупки чужого бота с чужим бюджетом.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.enums import Currency, Market
from app.services import strategy as strategy_service


@pytest.fixture(autouse=True)
def isolated_runtime(session, monkeypatch):
    """Изолировать общее хранилище настроек."""
    from contextlib import contextmanager

    from app.services import runtime, store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()
    monkeypatch.setattr(runtime, "trade_cap", lambda _m: None)
    yield
    store.invalidate()


def _manual(session, name):
    """Обычная стратегия."""
    return strategy_service.create_strategy(session, name=name)


# --- взаимоисключение --------------------------------------------------


def test_feed_strategy_disables_all_others(session):
    """Включили канал — остальные гаснут: иначе сужение сканера бессмысленно."""
    a, b = _manual(session, "Обычная A"), _manual(session, "Обычная B")
    a.is_enabled = b.is_enabled = True
    feed_strategy = strategy_service.ensure_feed_strategy(session)

    turned_off = strategy_service.set_enabled(session, feed_strategy, True)

    assert feed_strategy.is_enabled is True
    assert a.is_enabled is False
    assert b.is_enabled is False
    assert set(turned_off) == {"Обычная A", "Обычная B"}


def test_manual_strategy_disables_feed(session):
    """И наоборот: включили обычную — канал выключается."""
    feed_strategy = strategy_service.ensure_feed_strategy(session)
    feed_strategy.is_enabled = True
    manual = _manual(session, "Обычная")

    turned_off = strategy_service.set_enabled(session, manual, True)

    assert manual.is_enabled is True
    assert feed_strategy.is_enabled is False
    assert turned_off == [strategy_service.FEED_STRATEGY_NAME]


def test_manual_strategies_coexist(session):
    """Обычные стратегии друг другу не мешают — гасится только канал."""
    a, b = _manual(session, "A"), _manual(session, "B")
    a.is_enabled = True

    turned_off = strategy_service.set_enabled(session, b, True)

    assert a.is_enabled is True
    assert b.is_enabled is True
    assert turned_off == []


def test_disabling_turns_nothing_else_off(session):
    """Выключение стратегии не трогает остальные."""
    a = _manual(session, "A")
    a.is_enabled = True
    b = _manual(session, "B")

    assert strategy_service.set_enabled(session, b, False) == []
    assert a.is_enabled is True


def test_only_one_feed_strategy(session):
    """Стратегия канала заводится один раз, а не плодится."""
    first = strategy_service.ensure_feed_strategy(session)
    second = strategy_service.ensure_feed_strategy(session)

    assert first.id == second.id
    assert first.kind == strategy_service.KIND_FEED


# --- подстановка коллекций --------------------------------------------


def _find(session, collection, price, value, *, realized=True, msg=1, num=1):
    """Находка в канале."""
    from app.models import FeedFind, utcnow

    session.add(
        FeedFind(
            message_id=msg,
            posted_at=utcnow() - dt.timedelta(hours=1),
            collection=collection,
            number=num,
            price=Decimal(str(price)),
            value=Decimal(str(value)),
            realized=realized,
        )
    )
    session.flush()


def test_collections_come_from_feed(session):
    """Коллекции стратегии подставляет канал, а не человек."""
    strategy_service.ensure_feed_strategy(session)
    _find(session, "Tama Gadget", 100, 215, msg=1)
    _find(session, "Toy Bear", 40, 50, msg=2)

    report = strategy_service.refresh_feed_collections(session)

    assert report["ok"] is True
    assert set(report["collections"]) == {"Tama Gadget", "Toy Bear"}
    assert set(strategy_service.feed_strategy(session).collections) == {
        "Tama Gadget",
        "Toy Bear",
    }


def test_empty_feed_leaves_strategy_alone(session):
    """Пустой канал не обнуляет уже подставленные коллекции."""
    strategy = strategy_service.ensure_feed_strategy(session)
    strategy.collections = ["Старое"]

    report = strategy_service.refresh_feed_collections(session)

    assert report["ok"] is False
    assert strategy.collections == ["Старое"]


def test_missing_strategy_reported(session):
    """Без заведённой стратегии — понятный отказ, а не падение."""
    report = strategy_service.refresh_feed_collections(session)
    assert report["ok"] is False


def test_price_cap_clamped_by_market_limit(session, monkeypatch):
    """Потолок цены не выходит за лимит площадки.

    Канал показывает покупки чужого бота с чужим бюджетом: повторять
    их размер вслепую — верный способ выйти за свои лимиты.
    """
    from app.services import marketdata, runtime

    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )
    # Лимит площадки — 2 TON, а в канале находка на 102.60 GRAM.
    monkeypatch.setattr(
        runtime, "trade_cap",
        lambda m: Decimal("2") if m is Market.PORTALS else None,
    )

    strategy = strategy_service.ensure_feed_strategy(session)
    strategy.markets = [Market.PORTALS.value]
    _find(session, "Tama Gadget", 100, 215, msg=1)

    strategy_service.refresh_feed_collections(session)

    # 2 TON × 65 = 130 Stars, а не 102.60 GRAM × 65 = 6669.
    assert Decimal(strategy.max_price_stars) == Decimal("130")


def test_cap_from_feed_when_market_has_no_limit(session, monkeypatch):
    """Без лимита площадки потолок берётся из самой дорогой находки."""
    from app.services import marketdata

    monkeypatch.setattr(
        marketdata, "to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )
    strategy = strategy_service.ensure_feed_strategy(session)
    strategy.markets = [Market.PORTALS.value]
    _find(session, "Tama Gadget", 100, 215, msg=1)

    strategy_service.refresh_feed_collections(session)

    assert Decimal(strategy.max_price_stars) == Decimal("100") * 65


def test_collections_limited_in_count(session):
    """Список коллекций ограничен: иначе сканер снова распылится."""
    strategy_service.ensure_feed_strategy(session)
    for i in range(20):
        _find(session, f"Коллекция {i}", 10, 10 + i, msg=i + 1)

    report = strategy_service.refresh_feed_collections(session, limit=5)

    assert len(report["collections"]) == 5


def test_migration_fills_kind_for_existing_strategies(tmp_path):
    """После обновления сервера у прежних стратегий вид не остаётся пустым.

    ALTER TABLE ADD COLUMN не проставляет значение по умолчанию в уже
    существующие строки, а модель объявляет `kind` обязательным. Если
    не дозаполнить, обновлённый сервер получит базу с NULL там, где код
    рассчитывает на строку.
    """
    from sqlalchemy import create_engine, inspect, text

    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}")

    # База «прошлой версии»: таблица стратегий без колонки kind.
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE strategies ("
                " id INTEGER PRIMARY KEY, name VARCHAR(64),"
                " is_enabled BOOLEAN)"
            )
        )
        conn.execute(
            text("INSERT INTO strategies (id, name, is_enabled) VALUES (1, 'Старая', 0)")
        )

    import app.db as db_module

    original = db_module.engine
    db_module.engine = engine
    try:
        db_module.sync_columns()
    finally:
        db_module.engine = original
        engine.dispose()

    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        columns = {c["name"] for c in inspect(engine).get_columns("strategies")}
        assert "kind" in columns

        kind = conn.execute(
            text("SELECT kind FROM strategies WHERE id = 1")
        ).scalar_one()
    engine.dispose()

    assert kind == strategy_service.KIND_MANUAL


def test_feed_strategy_not_confused_with_manual(session):
    """Обычная стратегия не опознаётся как стратегия канала."""
    manual = _manual(session, "Обычная")
    feed_target = strategy_service.ensure_feed_strategy(session)

    assert strategy_service.feed_strategy(session).id == feed_target.id
    assert manual.kind == strategy_service.KIND_MANUAL
