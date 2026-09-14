"""Адаптер Portals (portals.tg / portals-market.com).

Статус по аудиту ТЗ: публичного developer API и SLA нет, доступ идёт
через приватный TMA-контракт. Поэтому все возможности — EXPERIMENTAL,
а write включается только явным флагом в конфиге и никогда не попадает
в AUTO.

Как получить PORTALS_AUTH: см. docs/SETUP.md, раздел «Токены площадок».
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


class PortalsAdapter(HttpMarketAdapter):
    """Portals: чтение каталога и истории, опционально — торговля."""

    market = Market.PORTALS
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(
            base_url or settings.portals_base_url, auth or settings.portals_auth
        )
        has_auth = bool(self.auth)
        # Без токена доступен только анонимный каталог (и то не всегда).
        self.capabilities = {
            Capability.SEARCH: CapabilityStatus.EXPERIMENTAL,
            Capability.HISTORY: CapabilityStatus.EXPERIMENTAL,
            Capability.BALANCE: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
            Capability.INVENTORY: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
            # Write-контур намеренно закрыт: покупка через приватный API
            # без SLA — это неуправляемый риск потери средств.
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
        """Активные лоты Portals, отсортированные по цене."""
        self._require(Capability.SEARCH)
        params: dict[str, object] = {
            "offset": 0,
            "limit": min(limit, 100),
            "sort_by": "price asc",
        }
        if collection:
            params["filter_by_collections"] = collection
        if model:
            params["filter_by_models"] = model
        if backdrop:
            params["filter_by_backdrops"] = backdrop
        if symbol:
            params["filter_by_symbols"] = symbol
        if max_price is not None:
            params["max_price"] = str(max_price)

        data = await self.request("GET", "/nfts/search", params=params)
        out: list[ListingDTO] = []
        for item in dig(data, "results", "nfts", "items", "data"):
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "amount", "floor_price"))
            external_id = first(item, "id", "nft_id", "slug", "address")
            if price is None or price <= 0 or not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(item, "owner", "seller", "owner_address"),
                    raw={"portals": True},
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж Portals — источник для оценки и ликвидности."""
        self._require(Capability.HISTORY)
        params: dict[str, object] = {"offset": 0, "limit": min(limit, 100)}
        if collection:
            params["filter_by_collections"] = collection
        if model:
            params["filter_by_models"] = model

        data = await self.request("GET", "/market/actions/", params=params)
        out: list[SaleDTO] = []
        for item in dig(data, "actions", "results", "items", "data"):
            if not isinstance(item, dict):
                continue
            action = str(first(item, "type", "action", default="")).lower()
            if action and "buy" not in action and "sale" not in action:
                continue
            price = to_decimal(first(item, "amount", "price"))
            if price is None or price <= 0:
                continue
            raw_date = first(item, "created_at", "date", "timestamp")
            happened = dt.datetime.utcnow()
            if isinstance(raw_date, str):
                try:
                    happened = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    happened = happened.replace(tzinfo=None)
                except ValueError:
                    pass
            nft = item.get("nft") if isinstance(item.get("nft"), dict) else item
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "id", "action_id", default="") or ""),
                    gift=self.parse_gift(nft),
                    price=price,
                    currency=Currency.TON,
                    happened_at=happened,
                    raw={"action": action},
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс аккаунта Portals."""
        self._require(Capability.BALANCE)
        data = await self.request("GET", "/users/balance/")
        amount = to_decimal(first(data, "balance", "amount", "ton"), Decimal(0))
        return [
            BalanceDTO(market=self.market, currency=Currency.TON, amount=amount or Decimal(0))
        ]

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки на Portals."""
        self._require(Capability.INVENTORY)
        data = await self.request(
            "GET", "/nfts/owned", params={"offset": 0, "limit": 100}
        )
        out: list[ListingDTO] = []
        for item in dig(data, "nfts", "results", "items", "data"):
            if not isinstance(item, dict):
                continue
            external_id = first(item, "id", "nft_id", "slug")
            if not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=to_decimal(first(item, "price"), Decimal(0)) or Decimal(0),
                    currency=Currency.TON,
                    raw={"listed": bool(first(item, "price"))},
                )
            )
        return out

    async def reconcile(
        self, *, external_ref: str | None, gift_ref: GiftRef | None
    ) -> ExecutionResult:
        """Сверить владение подарком по собственному инвентарю."""
        self._require(Capability.RECONCILE)
        if not external_ref:
            return ExecutionResult(ok=None, detail="нет идентификатора лота")
        owned = await self.inventory()
        mine = any(item.external_id == external_ref for item in owned)
        return ExecutionResult(
            ok=mine,
            external_ref=external_ref,
            detail="есть в инвентаре" if mine else "в инвентаре не найден",
        )
