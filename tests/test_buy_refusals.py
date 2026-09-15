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
