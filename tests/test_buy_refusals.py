"""Тесты честного разбора отказов при покупке.

Отказ из-за нехватки средств раньше попадал в общий обработчик и
объявлялся «неизвестным исходом»: резерв оставался занятым, запускалась
сверка, а человеку сообщалось, что деньги могли уйти. Ничего из этого
не происходило — просто не хватило звёзд.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import BalanceDTO, Capability
from app.enums import Currency, Market
from app.services import executor


class _Adapter:
    """Площадка с заданным балансом."""

    def __init__(self, amount=None, *, supports=True, currency=Currency.STARS):
        self.amount = amount
        self._supports = supports
        self.currency = currency

    def supports(self, _capability: Capability) -> bool:
        return self._supports

    async def balance(self):
        """Баланс или отказ его отдать."""
        if isinstance(self.amount, Exception):
            raise self.amount
        return [
            BalanceDTO(
                market=Market.TELEGRAM,
                amount=Decimal(str(self.amount)),
                currency=self.currency,
            )
        ]


# --- предварительная проверка баланса ---------------------------------


@pytest.mark.asyncio
async def test_shortage_detected():
    """Нехватка средств замечается до попытки покупки."""
    result = await executor._balance_shortage(
        _Adapter(100), Decimal("500"), Currency.STARS
    )

    assert result is not None
    assert "не хватает средств" in result
    assert "500" in result and "100" in result


@pytest.mark.asyncio
async def test_enough_money_passes():
    """Когда средств хватает, покупка не задерживается."""
    assert await executor._balance_shortage(
        _Adapter(500), Decimal("500"), Currency.STARS
    ) is None


@pytest.mark.asyncio
async def test_other_currency_does_not_count():
    """GRAM на балансе не оплачивает покупку в Stars."""
    result = await executor._balance_shortage(
        _Adapter(100, currency=Currency.TON), Decimal("500"), Currency.STARS
    )

    assert result is not None


@pytest.mark.asyncio
async def test_unknown_balance_does_not_block():
    """Не удалось узнать баланс — решает площадка, а не мы."""
    assert await executor._balance_shortage(
        _Adapter(RuntimeError("нет связи")), Decimal("500"), Currency.STARS
    ) is None


@pytest.mark.asyncio
async def test_market_without_balance_support():
    """Площадка без баланса проверку не проходит и не мешает."""
    assert await executor._balance_shortage(
        _Adapter(supports=False), Decimal("500"), Currency.STARS
    ) is None


@pytest.mark.asyncio
async def test_message_names_the_real_wallet():
    """В отказе сказано, что деньги на площадках и в @wallet не годятся."""
    result = await executor._balance_shortage(
        _Adapter(0), Decimal("500"), Currency.STARS
    )

    assert "@wallet" in result


# --- классификация ошибок площадки ------------------------------------


def test_balance_error_is_clean_refusal():
    """BALANCE_TOO_LOW — отказ до списания, а не потерянный ответ."""
    refusal = executor._clean_refusal(RuntimeError("RPCError 400: BALANCE_TOO_LOW"))

    assert refusal is not None
    assert "не хватает Stars" in refusal


def test_sold_out_is_clean_refusal():
    """Проданный лот — обычный отказ."""
    assert executor._clean_refusal(
        RuntimeError("STARGIFT_NOT_AVAILABLE")
    ) is not None


def test_unknown_error_stays_unknown():
    """Незнакомая ошибка остаётся неизвестным исходом.

    Осторожность важнее удобства: деньги могли уйти.
    """
    assert executor._clean_refusal(RuntimeError("что-то странное")) is None


def test_timeout_stays_unknown():
    """Обрыв связи — всегда неизвестный исход."""
    assert executor._clean_refusal(TimeoutError("read timeout")) is None


def test_refusal_mentions_the_code():
    """В тексте виден код площадки: по нему можно искать причину."""
    assert "BALANCE_TOO_LOW" in executor._clean_refusal(
        RuntimeError("BALANCE_TOO_LOW")
    )


# --- проверка получателя переноса -------------------------------------


class _Entity:
    """Ответ Telegram о том, кто стоит за именем."""

    def __init__(self, **kwargs):
        self.first_name = kwargs.get("first_name", "Portals")
        self.last_name = ""
        self.username = kwargs.get("username", "GiftsToPortals")
        self.id = kwargs.get("id", 777)
        self.bot = kwargs.get("bot", True)
        self.verified = kwargs.get("verified", False)
        self.scam = kwargs.get("scam", False)
        self.fake = kwargs.get("fake", False)
        self.restricted = kwargs.get("restricted", False)


@pytest.fixture()
def target_env(session, monkeypatch):
    """Окружение для проверки получателя переноса."""
    from contextlib import contextmanager

    import app.db as db_module
    from app.adapters import telegram_gateway
    from app.services import secrets, store

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(db_module, "init_db", lambda: None)
    monkeypatch.setattr(db_module, "session_scope", scope)
    monkeypatch.setattr(secrets, "session_scope", scope)
    monkeypatch.setattr(store, "session_scope", scope)
    secrets.invalidate()
    store.invalidate()

    def install(entity):
        class _Client:
            async def get_entity(self, _name):
                if isinstance(entity, Exception):
                    raise entity
                return entity

        class _Gateway:
            async def client(self):
                return _Client()

            async def close(self):
                pass

        monkeypatch.setattr(telegram_gateway, "default_gateway", lambda: _Gateway())

    yield install
    secrets.invalidate()
    store.invalidate()


@pytest.mark.asyncio
async def test_missing_target_explains_where_to_look(target_env, capsys):
    """Без получателя сказано, где его взять и куда вписать."""
    from app import cli

    target_env(_Entity())
    assert await cli._transfer_target() == 1
    err = capsys.readouterr().err

    assert "Portals" in err and "Пополнить" in err


@pytest.mark.asyncio
async def test_resolved_target_is_described(target_env, capsys):
    """Показано, кто именно стоит за настройкой."""
    from app import cli
    from app.services import secrets

    secrets.set_value("PORTALS_DEPOSIT", "@GiftsToPortals")
    target_env(_Entity())

    assert await cli._transfer_target() == 0
    out = capsys.readouterr().out

    assert "@GiftsToPortals" in out
    assert "777" in out
    assert "Сверьте" in out


@pytest.mark.asyncio
async def test_scam_account_refused(target_env, capsys):
    """Помеченный мошенническим аккаунт — отказ, а не предупреждение."""
    from app import cli
    from app.services import secrets

    secrets.set_value("PORTALS_DEPOSIT", "@Подделка")
    target_env(_Entity(scam=True))

    assert await cli._transfer_target() == 1
    out = capsys.readouterr().out

    assert "ОПАСНО" in out
    assert "МОШЕННИЧЕСКИЙ" in out


@pytest.mark.asyncio
async def test_fake_account_refused(target_env, capsys):
    """Поддельный — тоже."""
    from app import cli
    from app.services import secrets

    secrets.set_value("PORTALS_DEPOSIT", "@Подделка")
    target_env(_Entity(fake=True))

    assert await cli._transfer_target() == 1
    assert "ПОДДЕЛЬНЫЙ" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unresolvable_target_refused(target_env, capsys):
    """Несуществующий получатель — это потерянный подарок."""
    from app import cli
    from app.services import secrets

    secrets.set_value("PORTALS_DEPOSIT", "@нет-такого")
    target_env(ValueError("No user has that username"))

    assert await cli._transfer_target() == 1
    assert "потерянный подарок" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_ton_address_as_target_refused(target_env, capsys):
    """Адрес кошелька вместо аккаунта — отказ с объяснением.

    Самая вероятная ошибка: Portals показывает адрес кошелька на видном
    месте и сам предупреждает «только GRAM и токены TON». Подарок —
    это NFT, он передаётся аккаунту Telegram, и отправка на адрес
    кошелька означала бы его потерю.
    """
    from app import cli
    from app.services import secrets

    secrets.set_value(
        "PORTALS_DEPOSIT", "UQBALOTljDHq-S5vTxCInxczTpE_mMAEN28COu7lYxeNEVE8"
    )
    target_env(_Entity())

    assert await cli._transfer_target() == 1
    err = capsys.readouterr().err

    assert "адрес кошелька TON" in err
    assert "только GRAM" in err
    assert "Гифты" in err


def test_ton_address_recognised():
    """Распознаются оба формата адреса и не задеваются имена аккаунтов."""
    from app.services.secrets import looks_like_ton_address

    assert looks_like_ton_address(
        "UQBALOTljDHq-S5vTxCInxczTpE_mMAEN28COu7lYxeNEVE8"
    )
    assert not looks_like_ton_address("@GiftsToPortals")
    assert not looks_like_ton_address("777000")
    assert not looks_like_ton_address("")
