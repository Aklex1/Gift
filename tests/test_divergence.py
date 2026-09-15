"""Тесты поиска расхождений между площадками.

Механика, которой не нужен прогноз: одна и та же модель стоит на
площадках по-разному, и обе цены названы рынком. Ошибиться можно
только в комиссиях и в переносе — их считает arbitrage.

Здесь проверяется обвязка: что обходятся все доступные площадки, что
молчание одной не рушит обход, и что связка не объявляется там, где
сравнивать не с чем.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import GiftRef, ListingDTO, SearchSkipped
from app.enums import Currency, Market
from app.services import arbitrage, divergence, marketdata, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    from contextlib import contextmanager

    import app.db as db_module

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr("app.services.divergence.session_scope", scope)
    store.invalidate()
    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("100"), "test"
    )
    from app.services import valuation

    valuation.seed_fee_schedules(session)
    session.flush()
    yield
    store.invalidate()


def _lot(market, model, price, ident="x"):
    return ListingDTO(
        market=market,
        external_id=ident,
        gift=GiftRef(collection="Light Sword", model=model),
        price=Decimal(str(price)),
        currency=Currency.TON,
    )


class _Adapter:
    def __init__(self, market, rows, fail=None):
        self.market = market
        self.rows = rows
        self.fail = fail
        self.asked = []

    def supports(self, _cap):
        return True

    async def search(self, *, collection=None, **_kwargs):
        self.asked.append(collection)
        if self.fail:
            raise self.fail
        return self.rows


def _stub(monkeypatch, per_market, collections=("Light Sword",)):
    monkeypatch.setattr(divergence, "get_adapter", lambda m: per_market[m])
    monkeypatch.setattr(divergence, "searchable_markets", lambda: list(per_market))
    monkeypatch.setattr(
        divergence, "watched_collections", lambda _s, _l: list(collections)
    )
    arbitrage.set_enabled(True)


# --- обвязка ------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default(session):
    """Выключенный поиск никуда не ходит."""
    arbitrage.set_enabled(False)

    report = await divergence.sweep()

    assert report["ok"] is False
    assert "выключен" in report["detail"]


@pytest.mark.asyncio
async def test_one_market_is_not_enough(session, monkeypatch):
    """С одной площадкой сравнивать не с чем, и это говорится прямо.

    Молчаливое «ничего не найдено» отправило бы искать поломку там,
    где её нет: нужна просто вторая площадка.
    """
    _stub(monkeypatch, {Market.PORTALS: _Adapter(Market.PORTALS, [])})

    report = await divergence.sweep()

    assert report["ok"] is False
    assert "portals" in report["detail"]


@pytest.mark.asyncio
async def test_all_markets_are_walked(session, monkeypatch):
    """Обходятся все доступные площадки, а не только площадки стратегии."""
    adapters = {
        Market.PORTALS: _Adapter(Market.PORTALS, []),
        Market.MRKT: _Adapter(Market.MRKT, []),
    }
    _stub(monkeypatch, adapters, collections=("Light Sword", "Lol Pop"))

    await divergence.sweep()

    assert adapters[Market.PORTALS].asked == ["Light Sword", "Lol Pop"]
    assert adapters[Market.MRKT].asked == ["Light Sword", "Lol Pop"]


@pytest.mark.asyncio
async def test_a_silent_market_does_not_stop_the_sweep(session, monkeypatch):
    """Недоступная площадка попадает в замечания, обход продолжается."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [_lot(Market.PORTALS, "Enforcer", 7)]),
        Market.MRKT: _Adapter(Market.MRKT, [], fail=RuntimeError("401")),
    })

    report = await divergence.sweep()

    assert report["ok"] is True
    assert any("mrkt" in note for note in report["notes"])


@pytest.mark.asyncio
async def test_skipped_collection_is_explained(session, monkeypatch):
    """Пропуск по имени коллекции объясняется, а не молчит."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [_lot(Market.PORTALS, "Enforcer", 7)]),
        Market.MRKT: _Adapter(
            Market.MRKT, [], fail=SearchSkipped("коллекция не найдена")
        ),
    })

    report = await divergence.sweep()

    assert any("не найдена" in note for note in report["notes"])


# --- сама находка -------------------------------------------------------


@pytest.mark.asyncio
async def test_spread_between_markets_is_found(session, monkeypatch):
    """Одна модель дешевле на одной площадке и дороже на другой."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(
            Market.PORTALS, [_lot(Market.PORTALS, "Enforcer", "7.0", "a")]
        ),
        Market.MRKT: _Adapter(
            Market.MRKT, [_lot(Market.MRKT, "Enforcer", "12.0", "b")]
        ),
    })
    store.set(arbitrage.KEY_MIN_ROI, "0.05")

    report = await divergence.sweep()

    assert report["found"] >= 1
    best = report["spreads"][0]
    assert best["buy_market"] == "portals"
    assert best["sell_market"] == "mrkt"


@pytest.mark.asyncio
async def test_same_market_is_not_a_spread(session, monkeypatch):
    """Два лота одной площадки связкой не считаются."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(Market.PORTALS, [
            _lot(Market.PORTALS, "Enforcer", "7.0", "a"),
            _lot(Market.PORTALS, "Enforcer", "12.0", "b"),
        ]),
        Market.MRKT: _Adapter(Market.MRKT, []),
    })

    report = await divergence.sweep()

    assert report["found"] == 0


@pytest.mark.asyncio
async def test_different_models_are_not_compared(session, monkeypatch):
    """Разные модели — разные товары, сравнивать их нельзя."""
    _stub(monkeypatch, {
        Market.PORTALS: _Adapter(
            Market.PORTALS, [_lot(Market.PORTALS, "Enforcer", "7.0", "a")]
        ),
        Market.MRKT: _Adapter(
            Market.MRKT, [_lot(Market.MRKT, "Windu", "12.0", "b")]
        ),
    })

    report = await divergence.sweep()

    assert report["found"] == 0


# --- о чём сообщать -----------------------------------------------------


def test_only_notable_spreads_are_told():
    """Уведомление уходит не о каждой находке.

    Сообщение о каждой мелочи превращается в шум, который перестают
    читать, — а вместе с ним перестают читать и важное.
    """
    rows = [{"net_roi": "8.0%"}, {"net_roi": "35.0%"}, {"net_roi": "26.0%"}]

    told = divergence.worth_telling(rows)

    assert [row["net_roi"] for row in told] == ["35.0%", "26.0%"]


def test_broken_roi_does_not_break_the_notice():
    """Испорченное значение не роняет уведомление, а пропускается."""
    assert divergence.worth_telling([{"net_roi": "—"}]) == []


# --- токен MRKT ---------------------------------------------------------


def test_mrkt_token_is_taken_from_a_cookie_string():
    """Токен MRKT живёт в куке, а копируют его по-разному.

    Площадка держит токен в куке access_token, а не в заголовке
    Authorization: скопированное из браузера значение отвергалось с
    401, хотя было верным. Принимаем оба вида, в каком бы его ни
    достали из инструментов разработчика.
    """
    from app.adapters.mrkt import cookie_token

    token = "ad6d4ab6-2f34-4a8d-ad20-89bbe7375efb"

    assert cookie_token(token) == token
    assert cookie_token(f"access_token={token}") == token
    assert cookie_token(f"other=1; access_token={token}; more=2") == token


def test_missing_mrkt_token_is_empty_not_garbage():
    """Пустое значение остаётся пустым, а не превращается в мусорный токен."""
    from app.adapters.mrkt import cookie_token

    assert cookie_token("") == ""
    assert cookie_token(None) == ""
    assert cookie_token("   ") == ""


def test_mrkt_sends_the_token_both_ways():
    """Токен уходит и кукой, и заголовком.

    Какой вид примет площадка, зависит от версии её API, а лишний
    заголовок ничего не стоит.
    """
    from app.adapters.mrkt import COOKIE_NAME, MrktAdapter

    adapter = MrktAdapter(base_url="https://mrkt.test", auth="abc")
    headers = adapter._headers()

    assert headers["Cookie"] == f"{COOKIE_NAME}=abc"
    assert headers["Authorization"] == "abc"
    assert headers["Referer"]
