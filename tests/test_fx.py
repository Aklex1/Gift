"""Тесты курсов валют.

Прежняя константа 400 Stars за TON завышала курс примерно в шесть раз:
лот за 4 TON выглядел как 1600 Stars вместо 260. Любое сравнение цен
между площадками было бессмысленным.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import Currency
from app.models import utcnow
from app.services import fx, marketdata, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Подменить БД и хранилище на тестовые."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    monkeypatch.setattr(fx, "session_scope", scope)
    monkeypatch.setattr("app.services.runtime._audit", lambda *a, **k: None)
    store.invalidate()
    yield
    store.invalidate()


def test_rate_derived_from_two_sources(session):
    """Курс Stars/TON выводится из цены TON и цены звезды."""
    # TON = 1.35 USD, звезда = 0.02 USD -> 67.5 звёзд за TON.
    fx.set_manual_star_usd(Decimal("0.02"))
    marketdata.record_fx(
        session, Currency.TON, Currency.USD, Decimal("1.35"), source="tonapi"
    )
    marketdata.record_fx(
        session, Currency.STARS, Currency.USD, Decimal("0.02"), source="вручную"
    )
    raw = Decimal("1.35") / Decimal("0.02")
    assert raw == Decimal("67.5")


def test_spread_lowers_the_rate(session):
    """Спред занижает курс, а не завышает.

    Заниженный курс делает сделку менее выгодной на бумаге — это
    безопасная сторона ошибки.
    """
    fx.set_spread(Decimal("0.03"))
    raw = Decimal("67.5")
    adjusted = raw * (Decimal(1) - fx.spread())
    assert adjusted < raw
    assert adjusted == Decimal("65.475")


def test_spread_is_bounded(session):
    """Нелепое значение спреда игнорируется."""
    store.set("FX_SPREAD", "5")
    assert fx.spread() == fx.DEFAULT_SPREAD
    store.set("FX_SPREAD", "-1")
    assert fx.spread() == fx.DEFAULT_SPREAD


def test_without_a_rate_there_is_no_conversion(session):
    """Без курса цена в GRAM не пересчитывается вовсе.

    Раньше подставлялась грубая оценка. Но курс — множитель для каждой
    цены в GRAM: пока он неверен, сделки выглядят тем выгоднее, чем
    сильнее он врёт. Не показать ничего честнее, чем показать ROI в
    сорок тысяч процентов.
    """
    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) is None

    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("65.5"), source="tonapi"
    )
    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) == Decimal("262.0")


def test_stale_rate_is_not_a_rate(session):
    """Курс месячной давности к пересчёту не допускается.

    Снапшот не перезаписывается, когда источник недоступен, поэтому
    давнее значение продолжает лежать в базе и выглядеть рабочим. Так
    на сервере месяцами жил курс 400 звёзд за GRAM.
    """
    import datetime as dt

    from app.models import FxSnapshot, utcnow

    session.add(
        FxSnapshot(
            base=Currency.TON, quote=Currency.STARS, rate=Decimal("400"),
            source="default", taken_at=utcnow() - dt.timedelta(days=40),
        )
    )
    session.flush()

    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) is None


def test_fresh_rate_is_used(session):
    """Свежий курс работает как прежде."""
    marketdata.record_fx(
        session, Currency.TON, Currency.STARS, Decimal("100"), source="tonapi"
    )

    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) == Decimal("400")


def test_stale_inverse_rate_is_refused_too(session):
    """Обратный курс проверяется на свежесть наравне с прямым."""
    import datetime as dt

    from app.models import FxSnapshot, utcnow

    session.add(
        FxSnapshot(
            base=Currency.STARS, quote=Currency.TON, rate=Decimal("0.01"),
            source="test", taken_at=utcnow() - dt.timedelta(days=40),
        )
    )
    session.flush()

    assert marketdata.to_stars(session, Decimal("4"), Currency.TON) is None


def test_manual_star_price_can_be_cleared(session):
    """Ручная цена звезды снимается."""
    fx.set_manual_star_usd(Decimal("0.02"))
    assert fx.manual_star_usd() == Decimal("0.02")
    fx.set_manual_star_usd(None)
    assert fx.manual_star_usd() is None


def test_invalid_manual_price_ignored(session):
    """Мусор вместо цены не ломает расчёт."""
    store.set(fx.MANUAL_STAR_USD_KEY, "не число")
    assert fx.manual_star_usd() is None
    store.set(fx.MANUAL_STAR_USD_KEY, "-5")
    assert fx.manual_star_usd() is None


# --- правдоподобие цены звезды ----------------------------------------
#
# Курс Stars/GRAM выводится делением курса GRAM на цену звезды.
# Ошибка в цене звезды множит на себя каждую цену в GRAM: floor модели
# с Portals прилетает в расчёт раздутым, ROI рисуется сотнями
# процентов, и по числам это неотличимо от настоящей находки.


class _Option:
    """Пакет пополнения в том виде, в каком его отдаёт Telegram."""

    def __init__(self, stars, amount, currency="USD"):
        self.stars = stars
        self.amount = amount
        self.currency = currency


async def _price(monkeypatch, options):
    """Цена звезды по этим пакетам."""
    from app.services import fx

    class _Gateway:
        async def call(self, _request):
            return options

    class _Adapter:
        gateway = _Gateway()

    monkeypatch.setattr(
        "app.adapters.registry.get_adapter", lambda _market: _Adapter()
    )
    return await fx.fetch_stars_price_usd()


@pytest.mark.asyncio
async def test_cheapest_plausible_package_wins(monkeypatch):
    """Из нормальных пакетов берётся самый выгодный."""
    price = await _price(monkeypatch, [
        _Option(100, 199),      # 1.99 $ -> 0.0199 за звезду
        _Option(1000, 1500),    # 15 $   -> 0.015
    ])

    assert price == Decimal("0.015")


@pytest.mark.asyncio
async def test_absurdly_cheap_package_ignored(monkeypatch):
    """Пакет с неправдоподобной ценой не утягивает за собой курс.

    Берётся минимум, поэтому одна запись со странным номиналом
    задавала бы курс в одиночку — и завышала бы оценку всего, что
    номинировано в GRAM, во столько же раз.
    """
    price = await _price(monkeypatch, [
        _Option(1000, 340),     # 0.0034 за звезду — так Telegram не продаёт
        _Option(1000, 1500),
    ])

    assert price == Decimal("0.015")


@pytest.mark.asyncio
async def test_no_plausible_package_is_an_error(monkeypatch):
    """Если правдоподобных пакетов нет — это ошибка, а не курс.

    Записать сомнительный курс молча хуже, чем остаться со старым:
    старый хотя бы был верен когда-то.
    """
    from app.services import fx

    with pytest.raises(ValueError):
        await _price(monkeypatch, [_Option(1000, 1)])

    assert fx.STAR_USD_MIN < Decimal("0.015") < fx.STAR_USD_MAX


def test_manual_price_out_of_range_ignored(monkeypatch):
    """Опечатка в ручной цене звезды не уходит в расчёты."""
    from app.services import fx, store

    values: dict[str, str] = {}
    monkeypatch.setattr(store, "get", lambda key: values.get(key))

    values[fx.MANUAL_STAR_USD_KEY] = "0.0001"
    assert fx.manual_star_usd() is None

    values[fx.MANUAL_STAR_USD_KEY] = "0.015"
    assert fx.manual_star_usd() == Decimal("0.015")


def test_warning_names_the_direction_of_the_error():
    """Предупреждение говорит, в какую сторону поехали оценки."""
    from app.services import fx

    assert fx.rate_warning(Decimal("0.015")) is None
    assert "завышен" in fx.rate_warning(Decimal("0.0003"))
    assert "занижен" in fx.rate_warning(Decimal("0.9"))
    assert fx.rate_warning(None) is None


def test_stale_rate_is_flagged(session):
    """Давний курс помечается: он не перезаписывается сам собой.

    Обновление молча пропускается, когда источник недоступен, поэтому
    значение месячной давности продолжает лежать в таблице и выглядеть
    рабочим — а по нему считается каждая сделка.
    """
    import datetime as dt

    from app.models import FxSnapshot

    session.add(
        FxSnapshot(
            base=Currency.TON, quote=Currency.STARS, rate=Decimal("400"),
            source="default", taken_at=utcnow() - dt.timedelta(days=40),
        )
    )
    session.flush()

    data = fx.snapshot(session)
    row = next(r for r in data["pairs"] if r["title"] == "GRAM → Stars")

    assert row["stale"] is True
    assert row["age_days"] >= 40
    assert "не обновлялся" in data["warning"]


def test_fresh_rate_is_not_flagged(session):
    """Свежий курс ничем не помечается."""
    from app.models import FxSnapshot

    session.add(
        FxSnapshot(
            base=Currency.TON, quote=Currency.STARS, rate=Decimal("110"),
            source="tonapi", taken_at=utcnow(),
        )
    )
    session.flush()

    data = fx.snapshot(session)
    row = next(r for r in data["pairs"] if r["title"] == "GRAM → Stars")

    assert row["stale"] is False
    assert data["warning"] is None
