"""TON-клиент: только чтение.

По разделу «Signing/security» ТЗ приватный ключ кошелька в этом
приложении не хранится и не используется. Здесь — баланс, транзакции
и состояние NFT для сверки портфеля.

Подпись расходных операций делается либо вручную владельцем, либо
через TON Connect / внешний signer — это отдельный контур.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx

from app.config import settings
from app.enums import Currency

log = logging.getLogger(__name__)

NANO = Decimal("1000000000")


class TonClient:
    """Read-only клиент TonAPI."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or settings.tonapi_base_url).rstrip("/")
        self.api_key = api_key or settings.tonapi_key
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        """Задан ли адрес кошелька для наблюдения."""
        return bool(settings.ton_wallet_address)

    async def _http(self) -> httpx.AsyncClient:
        """Ленивый HTTP-клиент."""
        if self._client is None or self._client.is_closed:
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=20.0, headers=headers
            )
        return self._client

    async def balance(self, address: str | None = None) -> Decimal:
        """Баланс кошелька в TON."""
        addr = address or settings.ton_wallet_address
        if not addr:
            return Decimal(0)
        client = await self._http()
        response = await client.get(f"/v2/accounts/{addr}")
        response.raise_for_status()
        data = response.json()
        return Decimal(data.get("balance", 0)) / NANO

    async def transactions(self, address: str | None = None, limit: int = 50) -> list[dict]:
        """Последние транзакции кошелька — для сверки пополнений."""
        addr = address or settings.ton_wallet_address
        if not addr:
            return []
        client = await self._http()
        response = await client.get(
            f"/v2/blockchain/accounts/{addr}/transactions", params={"limit": limit}
        )
        response.raise_for_status()
        out: list[dict] = []
        for tx in response.json().get("transactions", []):
            out.append(
                {
                    "hash": tx.get("hash"),
                    "at": dt.datetime.utcfromtimestamp(tx.get("utime", 0)),
                    "success": tx.get("success"),
                    "currency": Currency.TON.value,
                }
            )
        return out

    async def nft_owner(self, nft_address: str) -> str | None:
        """Текущий владелец NFT — используется для сверки позиции."""
        client = await self._http()
        response = await client.get(f"/v2/nfts/{nft_address}")
        response.raise_for_status()
        owner = response.json().get("owner") or {}
        return owner.get("address")

    async def close(self) -> None:
        """Закрыть соединение."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
