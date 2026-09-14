"""Адаптер MRKT (mrkt.land).

Статус по аудиту ТЗ: публичного developer API и SLA нет. Все
возможности EXPERIMENTAL, write закрыт.
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
from app.enums import Currency, Market

log = logging.getLogger(__name__)


class MrktAdapter(HttpMarketAdapter):
    """MRKT: чтение каталога и истории сделок."""

    market = Market.MRKT
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(base_url or settings.mrkt_base_url, auth or settings.mrkt_auth)
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
            Capability.RECONCILE: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
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
        """Активные лоты MRKT."""
        self._require(Capability.SEARCH)
        payload: dict[str, object] = {
            "page": 1,
            "limit": min(limit, 100),
            "sortBy": "price",
            "sortDirection": "asc",
        }
        if collection:
            payload["collections"] = [collection]
        if model:
            payload["models"] = [model]
        if backdrop:
            payload["backdrops"] = [backdrop]
        if symbol:
            payload["symbols"] = [symbol]
        if max_price is not None:
            payload["maxPrice"] = float(max_price)

        data = await self.request("POST", "/gifts/search", json=payload)
        out: list[ListingDTO] = []
        for item in dig(data, "items", "gifts", "results", "data"):
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "salePrice", "amount"))
            external_id = first(item, "id", "giftId", "slug", "address")
            if price is None or price <= 0 or not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(item, "seller", "owner"),
                    raw={"mrkt": True},
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж MRKT."""
        self._require(Capability.HISTORY)
        payload: dict[str, object] = {"page": 1, "limit": min(limit, 100), "type": "sale"}
        if collection:
            payload["collections"] = [collection]
        if model:
            payload["models"] = [model]

        data = await self.request("POST", "/gifts/history", json=payload)
        out: list[SaleDTO] = []
        for item in dig(data, "items", "history", "results", "data"):
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "amount"))
            if price is None or price <= 0:
                continue
            raw_date = first(item, "date", "createdAt", "timestamp")
            happened = dt.datetime.utcnow()
            if isinstance(raw_date, (int, float)):
                happened = dt.datetime.utcfromtimestamp(float(raw_date) / (1000 if raw_date > 1e11 else 1))
            elif isinstance(raw_date, str):
                try:
                    happened = dt.datetime.fromisoformat(
                        raw_date.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                except ValueError:
                    pass
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "id", default="") or ""),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    happened_at=happened,
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс аккаунта MRKT."""
        self._require(Capability.BALANCE)
        data = await self.request("GET", "/user/balance")
        amount = to_decimal(first(data, "balance", "ton", "amount"), Decimal(0))
        return [
            BalanceDTO(market=self.market, currency=Currency.TON, amount=amount or Decimal(0))
        ]

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки на MRKT."""
        self._require(Capability.INVENTORY)
        data = await self.request("GET", "/user/gifts", params={"page": 1, "limit": 100})
        out: list[ListingDTO] = []
        for item in dig(data, "items", "gifts", "results", "data"):
            if not isinstance(item, dict):
                continue
            external_id = first(item, "id", "giftId", "slug")
            if not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=to_decimal(first(item, "price"), Decimal(0)) or Decimal(0),
                    currency=Currency.TON,
                )
            )
        return out

    async def reconcile(
        self, *, external_ref: str | None, gift_ref: GiftRef | None
    ) -> ExecutionResult:
        """Сверить владение по инвентарю MRKT."""
        self._require(Capability.RECONCILE)
        if not external_ref:
            return ExecutionResult(ok=None, detail="нет идентификатора лота")
        owned = await self.inventory()
        mine = any(item.external_id == external_ref for item in owned)
        return ExecutionResult(ok=mine, external_ref=external_ref)
