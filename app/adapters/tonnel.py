"""Адаптер Tonnel (marketplace.tonnel.network).

Статус по аудиту ТЗ: публичной документации нет, доступ прикрыт
Cloudflare. Возможности EXPERIMENTAL, write закрыт.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from app.adapters.base import (
    BalanceDTO,
    Capability,
    CapabilityStatus,
    ExecutionResult,
    GiftRef,
    ListingDTO,
    SaleDTO,
)
from app.adapters.http_base import HttpMarketAdapter, dig, first, to_decimal
from app.config import settings
from app.services import secrets
from app.enums import Currency, Market

log = logging.getLogger(__name__)


class TonnelAdapter(HttpMarketAdapter):
    """Tonnel: чтение каталога и активности."""

    market = Market.TONNEL
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(
            base_url or settings.tonnel_base_url,
            auth or secrets.resolve("TONNEL_AUTH", settings.tonnel_auth),
        )
        has_auth = bool(self.auth)
        self.capabilities = {
            Capability.SEARCH: CapabilityStatus.EXPERIMENTAL,
            Capability.HISTORY: CapabilityStatus.EXPERIMENTAL,
            Capability.BALANCE: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
            Capability.INVENTORY: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
            Capability.BUY: CapabilityStatus.UNAVAILABLE,
            Capability.LIST: CapabilityStatus.UNAVAILABLE,
            Capability.REPRICE: CapabilityStatus.UNAVAILABLE,
            Capability.CANCEL: CapabilityStatus.UNAVAILABLE,
            Capability.RECONCILE: CapabilityStatus.UNAVAILABLE,
        }

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
        """Активные лоты Tonnel."""
        self._require(Capability.SEARCH)
        gift_filter: dict[str, object] = {"price": {"$exists": True}}
        if collection:
            gift_filter["gift_name"] = collection
        if model:
            gift_filter["model"] = model
        if backdrop:
            gift_filter["backdrop"] = backdrop
        if symbol:
            gift_filter["symbol"] = symbol

        payload = {
            "page": 1,
            "limit": min(limit, 100),
            "sort": '{"price":1}',
            "filter": gift_filter,
        }
        data = await self.request("POST", "/pageGifts", json=payload)
        out: list[ListingDTO] = []
        for item in dig(data, "results", "gifts", "items", "data"):
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "amount"))
            external_id = first(item, "gift_id", "_id", "id", "name")
            if price is None or price <= 0 or not external_id:
                continue
            if max_price is not None and price > max_price:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(item, "owner", "seller"),
                    raw={"tonnel": True},
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж Tonnel."""
        self._require(Capability.HISTORY)
        payload: dict[str, object] = {
            "page": 1,
            "limit": min(limit, 100),
            "filter": {"type": "sale"},
        }
        if collection:
            payload["filter"] = {"type": "sale", "gift_name": collection}

        data = await self.request("POST", "/saleHistory", json=payload)
        out: list[SaleDTO] = []
        for item in dig(data, "results", "history", "items", "data"):
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "amount"))
            if price is None or price <= 0:
                continue
            raw_date = first(item, "date", "timestamp", "createdAt")
            happened = dt.datetime.utcnow()
            if isinstance(raw_date, str):
                try:
                    happened = dt.datetime.fromisoformat(
                        raw_date.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "_id", "id", default="") or ""),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    happened_at=happened,
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс аккаунта Tonnel."""
        self._require(Capability.BALANCE)
        data = await self.request("GET", "/balance")
        amount = to_decimal(first(data, "balance", "ton", "amount"), Decimal(0))
        return [
            BalanceDTO(market=self.market, currency=Currency.TON, amount=amount or Decimal(0))
        ]

    async def reconcile(
        self, *, external_ref: str | None, gift_ref: GiftRef | None
    ) -> ExecutionResult:
        """Сверка недоступна: write-контур Tonnel закрыт."""
        self._require(Capability.RECONCILE)
        return ExecutionResult(ok=None, detail="reconcile недоступен")
