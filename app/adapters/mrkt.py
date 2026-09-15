"""Адаптер MRKT (mrkt.info, API на api.tgmrkt.io).

Публичной документации на торговые операции у MRKT нет. Сообществом
описаны только авторизация и поиск:
    https://github.com/boostNT/MRKT-API

Поэтому здесь:
* чтение работает по документированным эндпоинтам;
* боевые операции открываются только через файл контракта
  markets/mrkt.json и флаг MRKT_ENABLE_WRITE — см. app/adapters/contracts.py.

Авторизация двухступенчатая: если задан MRKT_INIT_DATA (initData
мини-приложения), бот сам меняет его на токен и обновляет по
истечении. Иначе используется готовый токен из MRKT_AUTH.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

import httpx

from app.adapters.base import (
    BalanceDTO,
    Capability,
    CapabilityStatus,
    ExecutionResult,
    GiftRef,
    ListingDTO,
    SaleDTO,
)
from app.adapters.http_base import HttpMarketAdapter, ascii_header, dig, first, to_decimal
from app.config import settings
from app.services import runtime
from app.services import secrets
from app.enums import Currency, Market

log = logging.getLogger(__name__)

#: MRKT ожидает Referer своего CDN — без него запросы отклоняются.
CDN_REFERER = "https://cdn.tgmrkt.io/"

#: Имя куки, в которой площадка держит токен.
COOKIE_NAME = "access_token"


def cookie_token(value: str | None) -> str:
    """Выделить сам токен из того, что скопировали.

    Из браузера значение достают по-разному: кто-то копирует токен,
    кто-то целую строку куки. Разбирать это здесь дешевле, чем
    объяснять в интерфейсе, какой из двух видов «правильный».
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return part[len(COOKIE_NAME) + 1:].strip()
    # Строка без имени куки — значит это сам токен.
    return raw.split(";")[0].strip()


class MrktAdapter(HttpMarketAdapter):
    """MRKT: чтение каталога и истории, опционально — торговля."""

    market = Market.MRKT
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(
            base_url or settings.mrkt_base_url,
            auth or secrets.resolve("MRKT_AUTH", settings.mrkt_auth),
        )
        self._init_data = secrets.resolve("MRKT_INIT_DATA", settings.mrkt_init_data)
        has_auth = bool(self.auth or self._init_data)

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
        self.load_write_contract(
            enabled=bool(runtime.write_enabled(Market.MRKT) and has_auth)
        )

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """Заголовки MRKT: Referer своего CDN и токен в двух видах.

        Токен площадка держит в куке ``access_token``, а не в заголовке
        Authorization — из-за этого скопированное из браузера значение
        отвергалось с 401, хотя было верным. Шлём и куку, и заголовок:
        лишний заголовок ничего не стоит, а какой именно вид примут,
        зависит от версии их API.

        Значение принимается в любом виде, в каком его копируют: и
        голым токеном, и целой строкой куки ``access_token=...``.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; GiftTradingBot/0.1)",
            "Accept": "application/json",
            "Referer": CDN_REFERER,
        }
        token = cookie_token(self.auth)
        if token:
            headers["Authorization"] = ascii_header(token)
            headers["Cookie"] = ascii_header(f"{COOKIE_NAME}={token}")
        return headers

    async def ensure_token(self, *, force: bool = False) -> bool:
        """Обменять initData на токен.

        Returns:
            True, если токен есть или получен.
        """
        if self.auth and not force:
            return True
        if not self._init_data or force:
            # initData либо нет, либо он только что не сработал. Берём
            # свежий у Telegram: строка живёт часы, и держать её в
            # настройках вручную — гарантированная остановка торговли.
            from app.services import webauth

            try:
                self._init_data = await webauth.fetch_init_data(Market.MRKT)
                secrets.set_value("MRKT_INIT_DATA", self._init_data, actor="auto")
            except Exception as exc:  # noqa: BLE001 - остаёмся на старом
                log.warning("MRKT: не удалось получить initData: %s", exc)
        if not self._init_data:
            return bool(self.auth)

        # Обмен идёт отдельным клиентом: текущий может нести протухший токен.
        async with httpx.AsyncClient(base_url=self.base_url, timeout=20.0) as client:
            response = await client.post(
                "/auth",
                json={"data": self._init_data},
                headers={"Referer": CDN_REFERER, "Accept": "application/json"},
            )
        if response.status_code >= 400:
            log.error(
                "MRKT: авторизация не прошла (%s). Проверьте MRKT_INIT_DATA — "
                "он живёт недолго и берётся заново из мини-приложения.",
                response.status_code,
            )
            return False

        try:
            data = response.json()
        except ValueError:
            token = response.text.strip().strip('"')
            data = {"token": token} if token else {}

        token = first(data, "token", "accessToken", "access_token", "jwt")
        if not token and isinstance(data, str):
            token = data
        if not token:
            log.error("MRKT: ответ авторизации не содержит токена")
            return False

        self.auth = str(token)
        secrets.set_value("MRKT_AUTH", self.auth, actor="auto")
        # Пересоздаём клиент, чтобы заголовки обновились.
        await self.close()
        log.info("MRKT: токен получен")
        return True

    async def request(self, method: str, path: str, **kwargs):  # type: ignore[override]
        """Запрос с однократным обновлением токена при 401/403."""
        from app.adapters.base import AuthRequired

        await self.ensure_token()
        try:
            return await super().request(method, path, **kwargs)
        except AuthRequired:
            # Токен протух — меняем initData на новый и пробуем ещё раз.
            if not self._init_data or not await self.ensure_token(force=True):
                raise
            return await super().request(method, path, **kwargs)

    # ------------------------------------------------------------------
    # Чтение
    # ------------------------------------------------------------------
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
        """Активные лоты MRKT (документированный POST /gifts/saling)."""
        self._require(Capability.SEARCH)
        payload: dict[str, object] = {
            "collectionNames": [collection] if collection else [],
            "modelNames": [model] if model else [],
            "backdropNames": [backdrop] if backdrop else [],
            "symbolNames": [symbol] if symbol else [],
            "ordering": "Price",
            "lowToHigh": True,
            "maxPrice": float(max_price) if max_price is not None else None,
            "minPrice": None,
            "mintable": None,
            "number": None,
            "count": min(limit, 50),
            "cursor": "",
            "query": None,
            "promotedFirst": False,
        }

        data = await self.request("POST", "/gifts/saling", json=payload)
        out: list[ListingDTO] = []
        for item in dig(data, "gifts", "items", "results", "data"):
            if not isinstance(item, dict):
                continue
            # Цены MRKT приходят в нанотонах.
            raw_price = to_decimal(first(item, "salePrice", "price", "amount"))
            if raw_price is None or raw_price <= 0:
                continue
            price = raw_price / Decimal("1000000000") if raw_price > 10**6 else raw_price
            external_id = first(item, "id", "giftId", "name", "slug")
            if not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(item, "ownerId", "seller", "owner"),
                    raw={"mrkt": True},
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История продаж MRKT.

        Эндпоинт не документирован: при отказе площадки история просто
        не собирается, оценка строится на активных лотах.
        """
        self._require(Capability.HISTORY)
        payload: dict[str, object] = {
            "collectionNames": [collection] if collection else [],
            "modelNames": [model] if model else [],
            "count": min(limit, 50),
            "cursor": "",
        }
        data = await self.request("POST", "/gifts/sold", json=payload)
        out: list[SaleDTO] = []
        for item in dig(data, "gifts", "items", "results", "data"):
            if not isinstance(item, dict):
                continue
            raw_price = to_decimal(first(item, "salePrice", "price", "amount"))
            if raw_price is None or raw_price <= 0:
                continue
            price = raw_price / Decimal("1000000000") if raw_price > 10**6 else raw_price
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "id", "giftId", default="") or ""),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    happened_at=_parse_time(first(item, "soldAt", "date", "createdAt")),
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс аккаунта MRKT."""
        self._require(Capability.BALANCE)
        data = await self.request("GET", "/users/me")
        raw = to_decimal(first(data, "balance", "ton", "amount"), Decimal(0)) or Decimal(0)
        amount = raw / Decimal("1000000000") if raw > 10**6 else raw
        return [BalanceDTO(market=self.market, currency=Currency.TON, amount=amount)]

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки на MRKT."""
        self._require(Capability.INVENTORY)
        data = await self.request(
            "POST", "/gifts/my", json={"count": 50, "cursor": ""}
        )
        out: list[ListingDTO] = []
        for item in dig(data, "gifts", "items", "results", "data"):
            if not isinstance(item, dict):
                continue
            external_id = first(item, "id", "giftId", "name")
            if not external_id:
                continue
            raw_price = to_decimal(first(item, "salePrice", "price"), Decimal(0)) or Decimal(0)
            price = raw_price / Decimal("1000000000") if raw_price > 10**6 else raw_price
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    raw={"is_listed": price > 0},
                )
            )
        return out

    # ------------------------------------------------------------------
    # Боевые операции
    # ------------------------------------------------------------------
    async def buy(
        self, *, external_id: str, expected_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Купить лот, предварительно сверив цену."""
        self._require(Capability.BUY)

        fresh = await self.search(limit=50)
        match = next((x for x in fresh if x.external_id == external_id), None)
        if match is None:
            # Лот пропал из первой страницы — не рискуем.
            return ExecutionResult(
                ok=False, detail="лот не найден среди активных, покупка отменена"
            )
        if match.price != expected_price:
            return ExecutionResult(
                ok=False,
                detail=(
                    f"цена изменилась: ожидали {expected_price} TON, "
                    f"на площадке {match.price} TON"
                ),
            )
        return await self.execute_contract_op(
            "buy", external_id=external_id, price=expected_price
        )

    async def list_for_sale(
        self, *, gift_ref: GiftRef, price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Выставить подарок на продажу."""
        self._require(Capability.LIST)
        external_id = gift_ref.slug or gift_ref.nft_address or ""
        return await self.execute_contract_op(
            "list", external_id=external_id, price=price
        )

    async def reprice(
        self, *, external_id: str, new_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Изменить цену лота."""
        self._require(Capability.REPRICE)
        op = "reprice" if self.contract.has("reprice") else "list"
        return await self.execute_contract_op(
            op, external_id=external_id, price=new_price
        )

    async def cancel(self, *, external_id: str, idempotency_key: str) -> ExecutionResult:
        """Снять лот с продажи."""
        self._require(Capability.CANCEL)
        return await self.execute_contract_op("cancel", external_id=external_id)

    async def reconcile(
        self, *, external_ref: str | None, gift_ref: GiftRef | None
    ) -> ExecutionResult:
        """Сверить владение по инвентарю MRKT."""
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


def _parse_time(value: object) -> dt.datetime:
    """Разобрать время из ответа площадки."""
    if isinstance(value, (int, float)):
        seconds = float(value) / (1000 if value > 1e11 else 1)
        return dt.datetime.utcfromtimestamp(seconds)
    if isinstance(value, str):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            pass
    return dt.datetime.utcnow()
