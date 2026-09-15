"""Тесты отображения валюты.

15 июня 2026 сеть переименовала токен: TON стал GRAM, курс 1:1.
Человеку показывается GRAM, а внутри и во внешних запросах остаётся
TON — иначе пришлось бы переписывать каждую строку в базе и ломать
совместимость с TonAPI и площадками.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency, display_currency


# --- само правило ------------------------------------------------------


def test_ton_shows_as_gram():
    """TON человеку показывается как GRAM."""
    assert Currency.TON.display == "GRAM"
    assert display_currency(Currency.TON) == "GRAM"


def test_stars_unchanged():
    """Остальные валюты остаются собой."""
    assert Currency.STARS.display == "STARS"
    assert display_currency(Currency.USD) == "USD"
    assert display_currency(Currency.RUB) == "RUB"


def test_stored_value_is_still_ton():
    """В базе и в запросах к API валюта остаётся TON.

    Это и есть причина, по которой переименование сделано только в
    отображении: значение enum уходит в БД и во внешние сервисы.
    """
    assert Currency.TON.value == "TON"
    assert str(Currency.TON) == "TON"
    assert Currency("TON") is Currency.TON


def test_accepts_plain_strings():
    """Из базы валюта приходит строкой, и её тоже надо уметь показать."""
    assert display_currency("TON") == "GRAM"
    assert display_currency("ton") == "GRAM"
    assert display_currency("STARS") == "STARS"


def test_unknown_and_empty_do_not_crash():
    """Незнакомое значение отдаётся как есть, пустое — прочерком."""
    assert display_currency("XYZ") == "XYZ"
    assert display_currency(None) == "—"


# --- где это должно применяться ---------------------------------------


def test_panel_filter_registered():
    """В шаблонах есть фильтр, иначе пришлось бы править каждую подстановку."""
    from app.web.server import templates

    assert templates.env.filters["cur"]("TON") == "GRAM"


@pytest.mark.asyncio
async def test_market_balance_report_shows_gram(session, monkeypatch):
    """Сводка балансов площадок показывает GRAM, а не TON."""
    from contextlib import contextmanager

    from app.adapters.base import BalanceDTO, Capability
    from app.enums import Market
    from app.services import balances, store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()

    class _Adapter:
        """Площадка, которая отдаёт баланс в TON."""

        base_url = "https://example"
        native_currency = Currency.TON

        def supports(self, _capability: Capability) -> bool:
            return True

        async def balance(self):
            return [
                BalanceDTO(
                    market=Market.PORTALS,
                    amount=Decimal("3.5"),
                    currency=Currency.TON,
                )
            ]

    monkeypatch.setattr(balances, "get_adapter", lambda _m: _Adapter())

    report = await balances.refresh(markets=(Market.PORTALS,))

    assert "GRAM" in report["portals"]
    assert "TON" not in report["portals"]
    store.invalidate()


def test_arbitrage_report_shows_gram(session, monkeypatch):
    """Связка арбитража описывается в GRAM, а не в TON."""
    from app.adapters.base import GiftRef, ListingDTO
    from app.enums import Market
    from app.services import arbitrage, store

    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()
    monkeypatch.setattr(
        "app.services.marketdata.to_stars",
        lambda _s, amount, currency: (
            amount if currency is Currency.STARS else amount * Decimal("65")
        ),
    )

    def listing(market, price, ext):
        return ListingDTO(
            market=market,
            external_id=ext,
            gift=GiftRef(collection="pepe", model="neon"),
            price=Decimal(str(price)),
            currency=Currency.TON,
        )

    rows = [listing(Market.PORTALS, 10, "1"), listing(Market.MRKT, 16, "2")]
    data = arbitrage.find(session, rows)[0].as_dict()

    assert "GRAM" in data["buy_price_native"]
    assert "TON" not in data["buy_price_native"]
    store.invalidate()


def test_fx_pairs_titled_in_gram(session):
    """Названия валютных пар в панели — в GRAM."""
    from app.services import fx

    titles = [p["title"] for p in fx.snapshot(session)["pairs"]]

    assert "GRAM → Stars" in titles
    assert not any("TON" in t for t in titles)


@pytest.mark.parametrize(
    "path",
    [
        "app/web/templates/dashboard.html",
        "app/web/templates/accounts.html",
        "app/web/templates/trading.html",
    ],
)
def test_templates_do_not_show_ton(path):
    """В шаблонах не осталось надписей TON.

    Значения полей формы — отдельная история: там TON обязан
    остаться, потому что его читает Currency и пишет в базу.
    """
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if "TON" not in line:
            continue
        # Допустимо только как значение, уходящее в базу.
        assert 'value="TON"' in line or "== 'TON'" in line, line
