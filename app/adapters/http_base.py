"""Общая основа для адаптеров поверх HTTP.

Приватные API площадок (Portals, MRKT, Tonnel) не имеют публичной
документации и SLA. Поэтому:

* схема ответа не считается стабильной — парсинг устойчив к отсутствию
  полей и никогда не роняет процесс;
* по умолчанию доступно только чтение, write-возможности включаются
  явно в конфиге и всё равно остаются EXPERIMENTAL;
* сам факт недоступности не считается ошибкой приложения — capability
  помечается неработающей, торговля по этой площадке останавливается.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.adapters.base import (
    AuthRequired,
    GiftRef,
    MarketAdapter,
    OutcomeUnknown,
    RateLimited,
)

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
USER_AGENT = "Mozilla/5.0 (compatible; GiftTradingBot/0.1)"


def to_decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    """Мягко привести значение к Decimal."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def first(data: Any, *keys: str, default: Any = None) -> Any:
    """Взять первое непустое значение из словаря по списку ключей.

    Приватные API часто меняют имена полей — перебираем известные варианты.
    """
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return default


def dig(data: Any, *candidates: str) -> list:
    """Достать список записей из ответа неизвестной формы."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in candidates:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in candidates:
                inner = value.get(nested)
                if isinstance(inner, list):
                    return inner
    return []


class HttpMarketAdapter(MarketAdapter):
    """Адаптер площадки, работающей по HTTP."""

    #: Базовый URL берётся из конфига конкретным наследником.
    base_url: str = ""
    #: Значение заголовка авторизации (пустое = анонимный доступ).
    auth: str = ""
    #: Имя заголовка авторизации.
    auth_header: str = "Authorization"

    def __init__(self, base_url: str, auth: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth or ""
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """Заголовки запроса вместе с авторизацией."""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self.auth:
            headers[self.auth_header] = self.auth
        return headers

    async def _http(self) -> httpx.AsyncClient:
        """Ленивая инициализация HTTP-клиента."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=DEFAULT_TIMEOUT,
                headers=self._headers(),
                follow_redirects=True,
            )
        return self._client

    async def request(
        self,
        method: str,
        path: str,
        *,
        write: bool = False,
        retries: int = 2,
        **kwargs: Any,
    ) -> Any:
        """Выполнить запрос и вернуть разобранный JSON.

        Для write-операций обрыв связи превращается в OutcomeUnknown:
        повторять вслепую нельзя, деньги могли уйти.
        """
        client = await self._http()
        last_exc: Exception | None = None

        for attempt in range(retries + 1):
            try:
                response = await client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if write:
                    raise OutcomeUnknown(
                        f"{self.market.value}: связь оборвалась при {method} {path}: {exc}"
                    ) from exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise

            if response.status_code in (401, 403):
                raise AuthRequired(
                    f"{self.market.value}: нет доступа ({response.status_code}). "
                    f"Проверьте токен в конфиге."
                )
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "5") or 5)
                raise RateLimited(
                    f"{self.market.value}: слишком много запросов", retry_after=retry_after
                )
            if response.status_code >= 500 and attempt < retries and not write:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            response.raise_for_status()
            try:
                return response.json()
            except ValueError:
                # Cloudflare-заглушка или HTML вместо JSON.
                raise AuthRequired(
                    f"{self.market.value}: получен не-JSON ответ "
                    f"(вероятно защита Cloudflare или устаревший эндпоинт)"
                ) from None

        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------
    @staticmethod
    def parse_gift(item: dict) -> GiftRef:
        """Собрать GiftRef из записи произвольной формы.

        Общий разбор для всех приватных площадок: поля называются
        по-разному, но набор атрибутов у подарков Telegram одинаковый.
        """
        attrs = item.get("attributes") or {}
        if isinstance(attrs, list):
            # Формат [{trait_type: "Model", value: "..."}]
            flat: dict[str, Any] = {}
            for entry in attrs:
                if isinstance(entry, dict):
                    key = str(
                        first(entry, "trait_type", "type", "name", default="")
                    ).lower()
                    flat[key] = first(entry, "value", "name")
            attrs = flat

        collection = str(
            first(item, "collection", "collectionName", "gift_name", "giftName", "title", "name")
            or "unknown"
        )
        number = first(item, "number", "num", "gift_num", "index", "externalCollectionNumber")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None

        return GiftRef(
            collection=collection,
            number=number,
            slug=first(item, "slug", "name_slug", "gift_slug", "address"),
            model=first(item, "model", "modelName") or first(attrs, "model"),
            backdrop=first(item, "backdrop", "backdropName") or first(attrs, "backdrop"),
            symbol=(
                first(item, "symbol", "pattern", "symbolName")
                or first(attrs, "symbol", "pattern")
            ),
            nft_address=first(item, "address", "nft_address", "nftAddress"),
            attributes={"source": item.get("_source")},
        )

    async def close(self) -> None:
        """Закрыть HTTP-клиент."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
