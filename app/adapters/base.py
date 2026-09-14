"""Общий контракт адаптера площадки.

Раздел 6 ТЗ требует единый contract
search/history/balance/buy/list/reprice/cancel/reconcile
с флагами supported/experimental/unavailable.

Ключевые правила, зашитые в базовый класс:

* Любая операция, чья capability не SUPPORTED и не EXPERIMENTAL,
  завершается CapabilityUnavailable — без попытки сетевого вызова.
* Результат write-операции никогда не «додумывается»: таймаут даёт
  исход UNKNOWN, а не FAILED. Слепой retry запрещён — исход сводит
  reconcile.
"""

from __future__ import annotations

import abc
import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from app.enums import Capability, CapabilityStatus, Currency, Market

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Ошибки
# ----------------------------------------------------------------------
class AdapterError(Exception):
    """Базовая ошибка адаптера."""


class CapabilityUnavailable(AdapterError):
    """Операция не поддерживается этой площадкой в текущей конфигурации."""


class AuthRequired(AdapterError):
    """Нет или истекли учётные данные площадки."""


class RateLimited(AdapterError):
    """Площадка попросила притормозить."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class OutcomeUnknown(AdapterError):
    """Исход write-операции неизвестен.

    Поднимается при таймауте или обрыве ПОСЛЕ отправки запроса.
    Наверху это превращается в IntentStatus.UNKNOWN и уходит в сверку,
    а не в повторную покупку.
    """

    def __init__(self, message: str, external_ref: str | None = None) -> None:
        super().__init__(message)
        self.external_ref = external_ref


# ----------------------------------------------------------------------
# DTO
# ----------------------------------------------------------------------
@dataclass(slots=True)
class GiftRef:
    """Идентичность подарка в терминах площадки."""

    collection: str
    number: int | None = None
    slug: str | None = None
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    tg_gift_id: int | None = None
    nft_address: str | None = None
    attributes: dict = field(default_factory=dict)

    @property
    def canonical_key(self) -> str:
        """Стабильный ключ для схлопывания одного подарка между площадками."""
        if self.slug:
            return self.slug.lower()
        if self.number is not None:
            return f"{self.collection.lower().replace(' ', '')}#{self.number}"
        traits = "|".join(
            str(x or "") for x in (self.model, self.backdrop, self.symbol)
        )
        return f"{self.collection.lower().replace(' ', '')}|{traits}"


@dataclass(slots=True)
class ListingDTO:
    """Активный лот на площадке."""

    market: Market
    external_id: str
    gift: GiftRef
    price: Decimal
    currency: Currency
    seller: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class SaleDTO:
    """Состоявшаяся сделка."""

    market: Market
    external_id: str | None
    gift: GiftRef
    price: Decimal
    currency: Currency
    happened_at: dt.datetime
    buyer: str | None = None
    seller: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class BalanceDTO:
    """Баланс аккаунта на площадке."""

    market: Market
    currency: Currency
    amount: Decimal
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionResult:
    """Исход write-операции.

    ``ok=None`` означает UNKNOWN — исход надо свести отдельно.
    """

    ok: bool | None
    external_ref: str | None = None
    executed_price: Decimal | None = None
    currency: Currency | None = None
    detail: str | None = None
    raw: dict = field(default_factory=dict)


# ----------------------------------------------------------------------
# Базовый адаптер
# ----------------------------------------------------------------------
class MarketAdapter(abc.ABC):
    """Базовый класс площадки.

    Наследник объявляет ``market`` и словарь ``capabilities``.
    Все публичные операции сначала проходят ``_require``.
    """

    market: Market
    #: Матрица возможностей. Всё, что не объявлено, считается UNAVAILABLE.
    capabilities: dict[Capability, CapabilityStatus] = {}

    #: Валюта, в которой площадка номинирует цены по умолчанию.
    native_currency: Currency = Currency.STARS

    def status_of(self, capability: Capability) -> CapabilityStatus:
        """Текущий статус возможности."""
        return self.capabilities.get(capability, CapabilityStatus.UNAVAILABLE)

    def supports(self, capability: Capability) -> bool:
        """Можно ли вообще вызывать эту операцию."""
        return self.status_of(capability) is not CapabilityStatus.UNAVAILABLE

    def is_auto_safe(self, capability: Capability) -> bool:
        """Допустима ли операция в автономном режиме.

        Только SUPPORTED (официальный API). Приватные/reverse-engineered
        коннекторы в AUTO не пускаются — прямое требование аудита.
        """
        return self.status_of(capability) is CapabilityStatus.SUPPORTED

    def _require(self, capability: Capability) -> None:
        """Проверить возможность до сетевого вызова."""
        status = self.status_of(capability)
        if status is CapabilityStatus.UNAVAILABLE:
            raise CapabilityUnavailable(
                f"{self.market.value}: операция {capability.value} недоступна "
                f"(нет реализации, доступа или отключена в конфиге)"
            )

    # -- read ----------------------------------------------------------
    async def search(
        self,
        *,
        collection: str | None = None,
        model: str | None = None,
        backdrop: str | None = None,
        symbol: str | None = None,
        max_price: Decimal | None = None,
        limit: int = 100,
    ) -> list[ListingDTO]:
        """Активные лоты, отсортированные по цене."""
        self._require(Capability.SEARCH)
        raise CapabilityUnavailable(f"{self.market.value}: search не реализован")

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж для оценки и ликвидности."""
        self._require(Capability.HISTORY)
        raise CapabilityUnavailable(f"{self.market.value}: history не реализован")

    async def balance(self) -> list[BalanceDTO]:
        """Баланс аккаунта на площадке."""
        self._require(Capability.BALANCE)
        raise CapabilityUnavailable(f"{self.market.value}: balance не реализован")

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки."""
        self._require(Capability.INVENTORY)
        raise CapabilityUnavailable(f"{self.market.value}: inventory не реализован")

    # -- write ---------------------------------------------------------
    async def buy(
        self, *, external_id: str, expected_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Купить лот по точной ожидаемой цене.

        Реализация обязана заново прочитать лот и отказаться, если цена
        изменилась: «покупка по увиденной цене» не гарантируется площадкой.
        """
        self._require(Capability.BUY)
        raise CapabilityUnavailable(f"{self.market.value}: buy не реализован")

    async def list_for_sale(
        self, *, gift_ref: GiftRef, price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Выставить подарок на продажу."""
        self._require(Capability.LIST)
        raise CapabilityUnavailable(f"{self.market.value}: list не реализован")

    async def reprice(
        self, *, external_id: str, new_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Изменить цену активного лота."""
        self._require(Capability.REPRICE)
        raise CapabilityUnavailable(f"{self.market.value}: reprice не реализован")

    async def cancel(self, *, external_id: str, idempotency_key: str) -> ExecutionResult:
        """Снять лот с продажи."""
        self._require(Capability.CANCEL)
        raise CapabilityUnavailable(f"{self.market.value}: cancel не реализован")

    async def reconcile(self, *, external_ref: str | None, gift_ref: GiftRef | None) -> ExecutionResult:
        """Свести исход операции с реальным состоянием площадки.

        Вызывается для намерений в статусе UNKNOWN.
        """
        self._require(Capability.RECONCILE)
        raise CapabilityUnavailable(f"{self.market.value}: reconcile не реализован")

    # -- health --------------------------------------------------------
    async def probe(self) -> dict[Capability, tuple[bool, str]]:
        """Живая проверка доступности read-операций.

        Write-операции не пробуются — это тратило бы деньги.
        """
        results: dict[Capability, tuple[bool, str]] = {}
        if self.supports(Capability.SEARCH):
            try:
                rows = await self.search(limit=1)
                results[Capability.SEARCH] = (True, f"получено лотов: {len(rows)}")
            except Exception as exc:  # noqa: BLE001 - probe не должен падать
                results[Capability.SEARCH] = (False, f"{type(exc).__name__}: {exc}")
        if self.supports(Capability.BALANCE):
            try:
                bal = await self.balance()
                results[Capability.BALANCE] = (True, f"балансов: {len(bal)}")
            except Exception as exc:  # noqa: BLE001
                results[Capability.BALANCE] = (False, f"{type(exc).__name__}: {exc}")
        return results

    async def close(self) -> None:
        """Освободить сетевые ресурсы."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.market.value}>"
