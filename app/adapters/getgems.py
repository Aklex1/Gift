"""Адаптер Getgems (публичный API + TON on-chain).

По аудиту ТЗ: публичная документация есть, но write-операции требуют
подписи кошельком, а часть gift/off-chain функций доступна только
партнёрам. Поэтому здесь только чтение.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from app.adapters.base import (
    Capability,
    CapabilityStatus,
    ListingDTO,
    SaleDTO,
)
from app.adapters.http_base import HttpMarketAdapter, dig, first, to_decimal
from app.config import settings
from app.services import secrets
from app.enums import Currency, Market

log = logging.getLogger(__name__)

NANO = Decimal("1000000000")


class GetgemsAdapter(HttpMarketAdapter):
    """Getgems: публичное чтение листингов и истории."""

    market = Market.GETGEMS
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(
            base_url or settings.getgems_base_url,
            auth or secrets.resolve("GETGEMS_API_KEY", settings.getgems_api_key),
        )
        has_key = bool(self.auth)
        # Публичный API документирован — статус выше, чем у приватных площадок,
        # но покупка требует подписи кошельком, поэтому write закрыт.
        self.capabilities = {
            Capability.SEARCH: (
                CapabilityStatus.EXPERIMENTAL if has_key else CapabilityStatus.UNAVAILABLE
            ),
            Capability.HISTORY: (
                CapabilityStatus.EXPERIMENTAL if has_key else CapabilityStatus.UNAVAILABLE
            ),
            Capability.BUY: CapabilityStatus.UNAVAILABLE,
            Capability.LIST: CapabilityStatus.UNAVAILABLE,
            Capability.REPRICE: CapabilityStatus.UNAVAILABLE,
            Capability.CANCEL: CapabilityStatus.UNAVAILABLE,
            Capability.BALANCE: CapabilityStatus.UNAVAILABLE,
            Capability.INVENTORY: CapabilityStatus.UNAVAILABLE,
            Capability.RECONCILE: CapabilityStatus.UNAVAILABLE,
        }

    def _headers(self) -> dict[str, str]:
        """Getgems ждёт ключ в заголовке authorization без схемы."""
        headers = {"User-Agent": "GiftTradingBot/0.1", "Accept": "application/json"}
        if self.auth:
            headers["authorization"] = self.auth
        return headers

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
        """Активные листинги коллекции.

        ``collection`` здесь — адрес коллекции в TON, а не человекочитаемое
        имя: так устроен публичный API Getgems.
        """
        self._require(Capability.SEARCH)
        if not collection:
            return []
        data = await self.request(
            "GET",
            f"/v1/collection/{collection}/items",
            params={"limit": min(limit, 100), "offset": 0, "onSale": "true"},
        )
        out: list[ListingDTO] = []
        for item in dig(data, "items", "nftItems", "response", "data"):
            if not isinstance(item, dict):
                continue
            sale = item.get("sale") if isinstance(item.get("sale"), dict) else item
            price_raw = to_decimal(first(sale, "fullPrice", "price", "value"))
            if price_raw is None or price_raw <= 0:
                continue
            # Цены приходят в нанотонах.
            price = price_raw / NANO if price_raw > 10**6 else price_raw
            if max_price is not None and price > max_price:
                continue
            address = first(item, "address", "nftAddress", "id")
            if not address:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(address),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(sale, "owner", "ownerAddress"),
                    raw={"getgems": True},
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж коллекции (по адресу коллекции в TON)."""
        self._require(Capability.HISTORY)
        if not collection:
            return []
        data = await self.request(
            "GET",
            f"/v1/collection/{collection}/history",
            params={"limit": min(limit, 100), "offset": 0, "types": "sold"},
        )
        out: list[SaleDTO] = []
        for item in dig(data, "items", "history", "response", "data"):
            if not isinstance(item, dict):
                continue
            price_raw = to_decimal(first(item, "price", "fullPrice", "value"))
            if price_raw is None or price_raw <= 0:
                continue
            price = price_raw / NANO if price_raw > 10**6 else price_raw
            ts = first(item, "time", "timestamp", "date")
            happened = dt.datetime.utcnow()
            if isinstance(ts, (int, float)):
                happened = dt.datetime.utcfromtimestamp(
                    float(ts) / (1000 if ts > 1e11 else 1)
                )
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "hash", "id", default="") or ""),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    happened_at=happened,
                )
            )
        return out
