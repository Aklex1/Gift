"""Адаптер Portals (portals.tg).

Эндпоинты соответствуют фактическому API мини-приложения:

    GET  nfts/search?offset&limit&sort_by&status=listed&filter_by_*
    GET  nfts/owned?offset&limit
    GET  users/wallets/
    GET  market/actions/?offset&limit
    POST nfts                  {"nft_details": [{"id": ..., "price": "..."}]}
    POST nfts/bulk-list        {"nft_prices": [{"nft_id": ..., "price": "..."}]}
    POST nfts/{nft_id}/list    {"price": "..."}

Публичного developer API и SLA у площадки нет: контракт может
поменяться без предупреждения. Поэтому боевые операции по умолчанию
выключены и включаются PORTALS_ENABLE_WRITE, а пути при желании
переопределяются файлом markets/portals.json.
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
from app.adapters.http_base import HttpMarketAdapter, ascii_header, dig, first, to_decimal
from app.config import settings
from app.enums import Currency, Market
from app.services import runtime, secrets

log = logging.getLogger(__name__)

#: Значения по умолчанию для боевых операций.
#: Пользователю не нужно переносить их руками из DevTools, но при
#: смене API их можно переопределить в markets/portals.json.
DEFAULT_WRITE_CONTRACT = {
    "buy": {
        "method": "POST",
        "path": "/nfts",
        "body": {"nft_details": [{"id": "{external_id}", "price": "{price}"}]},
    },
    "list": {
        "method": "POST",
        "path": "/nfts/bulk-list",
        "body": {"nft_prices": [{"nft_id": "{external_id}", "price": "{price}"}]},
    },
    "reprice": {
        "method": "POST",
        "path": "/nfts/{external_id}/list",
        "body": {"price": "{price}"},
    },
}


#: Минимум между попытками продлить токен после 401.
RENEW_COOLDOWN_SEC = 120.0


def short_collection_name(collection: str) -> str:
    """Короткое имя коллекции в том виде, в каком его ждёт Portals.

    Площадка принимает `plushpepe`, а не `Plush Pepe`: на
    отображаемое имя эндпоинт отвечает пустыми списками — без ошибки,
    просто без данных. Из-за этого floor по моделям не приходил вовсе,
    оценка откатывалась на пустую историю продаж, и каждый лот Portals
    отбраковывался как «мало рыночных данных».
    """
    import re

    return re.sub(r"[^a-z0-9]", "", (collection or "").lower())


class PortalsAdapter(HttpMarketAdapter):
    """Portals: чтение каталога и, по явному разрешению, торговля."""

    market = Market.PORTALS
    native_currency = Currency.TON
    auth_header = "Authorization"

    def __init__(self, base_url: str | None = None, auth: str | None = None) -> None:
        super().__init__(
            base_url or settings.portals_base_url,
            auth or secrets.resolve("PORTALS_AUTH", settings.portals_auth),
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
            Capability.RECONCILE: (
                CapabilityStatus.EXPERIMENTAL if has_auth else CapabilityStatus.UNAVAILABLE
            ),
        }
        self.load_write_contract(
            enabled=bool(runtime.write_enabled(Market.PORTALS) and has_auth),
            defaults=DEFAULT_WRITE_CONTRACT,
        )
        #: Кэш floor по атрибутам: коллекция -> (когда, значения).
        self._floor_cache: dict[str, tuple[dt.datetime, dict]] = {}
        #: Когда последний раз продлевали токен. Адаптер живёт долго,
        #: поэтому «продлевали уже» должно истекать: иначе одна неудача
        #: навсегда выключила бы продление в этом процессе.
        self._renewed_at = 0.0

    # ------------------------------------------------------------------
    async def request(self, method: str, path: str, **kwargs):  # type: ignore[override]
        """Запрос с однократным продлением токена при 401/403.

        Токен Portals — initData мини-приложения, он живёт часы. Раньше
        его смерть означала остановку торговли до ручного вмешательства;
        теперь бот открывает мини-приложение сам и повторяет запрос.
        """
        import time

        from app.adapters.base import AuthRequired

        try:
            return await super().request(method, path, **kwargs)
        except AuthRequired:
            # Пауза между попытками продления: без неё поток отказов
            # превратился бы в поток запросов к Telegram и FloodWait.
            if time.monotonic() - self._renewed_at < RENEW_COOLDOWN_SEC:
                raise
            self._renewed_at = time.monotonic()

            from app.services import webauth

            report = await webauth.renew(Market.PORTALS)
            if not report["ok"]:
                raise
            self.auth = secrets.resolve("PORTALS_AUTH", settings.portals_auth)
            # Клиент держит старый заголовок — пересоздаём.
            await self.close()
            log.info("Portals: токен продлён, повторяю %s %s", method, path)
            return await super().request(method, path, **kwargs)

    def _headers(self) -> dict[str, str]:
        """Portals отвергает запросы без Origin и Referer своего домена."""
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
            "Origin": "https://portals.tg",
            "Referer": "https://portals.tg/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
            ),
        }
        if self.auth:
            # Значение копируется целиком вместе с префиксом "tma ".
            # ascii_header чинит токен, скопированный из DevTools в
            # расшифрованном виде: кириллица в заголовок не проходит.
            headers["Authorization"] = ascii_header(self.auth)
        return headers

    @staticmethod
    def parse_gift(item: dict) -> GiftRef:
        """Разобрать подарок Portals.

        Атрибуты приходят списком вида
        ``[{"type": "model", "value": "...", "rarity_per_mille": 15}]``.
        """
        traits: dict[str, str] = {}
        rarity: dict[str, float] = {}
        for attr in item.get("attributes") or []:
            if not isinstance(attr, dict):
                continue
            kind = str(attr.get("type") or "").lower()
            if not kind:
                continue
            traits[kind] = attr.get("value")
            per_mille = attr.get("rarity_per_mille")
            if per_mille is not None:
                try:
                    rarity[f"{kind}_rarity"] = round(float(per_mille) / 1000.0, 6)
                except (TypeError, ValueError):
                    pass

        number = first(item, "external_collection_number", "number", "num")
        try:
            number = int(number) if number is not None else None
        except (TypeError, ValueError):
            number = None

        return GiftRef(
            collection=str(first(item, "name", "collection", default="unknown")),
            number=number,
            slug=first(item, "id", "nft_id"),
            model=traits.get("model"),
            backdrop=traits.get("backdrop"),
            symbol=traits.get("symbol"),
            nft_address=first(item, "address", "nft_address"),
            attributes={
                **rarity,
                "collection_id": item.get("collection_id"),
                "floor_price": item.get("floor_price"),
                "status": item.get("status"),
                # До этого момента подарок нельзя перепродать.
                "unlocks_at": item.get("unlocks_at"),
            },
        )

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
        """Активные лоты, отсортированные по возрастанию цены."""
        self._require(Capability.SEARCH)
        # Площадка отдаёт максимум сотню лотов за запрос, поэтому за
        # большей выборкой идём страницами. Без этого проход видел
        # только сотню самых дешёвых лотов коллекции, а недооценка
        # встречается и выше по цене.
        page_size = 100
        params: dict[str, object] = {
            "offset": 0,
            "limit": page_size,
            "sort_by": "price asc",
            "status": "listed",
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
            params["min_price"] = 0
            params["max_price"] = float(max_price)

        out: list[ListingDTO] = []
        seen: set[str] = set()
        for offset in range(0, max(limit, page_size), page_size):
            params["offset"] = offset
            data = await self.request("GET", "/nfts/search", params=params)
            rows = dig(data, "results", "nfts", "items", "data") or []
            page = self._to_listings(rows)
            if not page:
                break

            # Площадка иногда повторяет записи между страницами, если
            # в этот момент кто-то купил лот и выкладка сдвинулась.
            for listing in page:
                if listing.external_id in seen:
                    continue
                seen.add(listing.external_id)
                out.append(listing)

            if len(rows) < page_size or len(out) >= limit:
                break
        return out[:limit]

    def _to_listings(self, rows: list) -> list[ListingDTO]:
        """Превратить записи Portals в лоты."""
        out: list[ListingDTO] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            price = to_decimal(first(item, "price", "amount"))
            external_id = first(item, "id", "nft_id")
            if price is None or price <= 0 or not external_id:
                continue
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    seller=first(item, "owner_id", "owner", "seller"),
                    raw={
                        "status": item.get("status"),
                        "floor_price": item.get("floor_price"),
                    },
                )
            )
        return out

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История сделок площадки — основа для оценки и ликвидности."""
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
            if action and not any(k in action for k in ("buy", "sale", "sold")):
                continue
            price = to_decimal(first(item, "amount", "price"))
            if price is None or price <= 0:
                continue
            nft = item.get("nft") if isinstance(item.get("nft"), dict) else item
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(first(item, "id", "action_id", default="") or ""),
                    gift=self.parse_gift(nft),
                    price=price,
                    currency=Currency.TON,
                    happened_at=_parse_time(
                        first(item, "created_at", "date", "timestamp")
                    ),
                    raw={"action": action},
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс кошельков аккаунта на площадке."""
        self._require(Capability.BALANCE)
        data = await self.request("GET", "/users/wallets/")

        rows = dig(data, "wallets", "results", "items", "data")
        out: list[BalanceDTO] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            amount = to_decimal(first(item, "balance", "amount"), Decimal(0))
            if amount is None:
                continue
            out.append(
                BalanceDTO(market=self.market, currency=Currency.TON, amount=amount)
            )
        if not out:
            # Ответ может быть одиночным объектом, а не списком.
            amount = to_decimal(first(data, "balance", "amount", "ton"), Decimal(0))
            out.append(
                BalanceDTO(
                    market=self.market, currency=Currency.TON, amount=amount or Decimal(0)
                )
            )
        return out

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки на Portals."""
        self._require(Capability.INVENTORY)
        data = await self.request(
            "GET", "/nfts/owned", params={"offset": 0, "limit": 100}
        )
        out: list[ListingDTO] = []
        for item in dig(data, "results", "nfts", "items", "data"):
            if not isinstance(item, dict):
                continue
            external_id = first(item, "id", "nft_id")
            if not external_id:
                continue
            price = to_decimal(first(item, "price"), Decimal(0)) or Decimal(0)
            status = str(item.get("status") or "").lower()
            out.append(
                ListingDTO(
                    market=self.market,
                    external_id=str(external_id),
                    gift=self.parse_gift(item),
                    price=price,
                    currency=Currency.TON,
                    raw={
                        "is_listed": status == "listed" or price > 0,
                        "unlocks_at": item.get("unlocks_at"),
                        "status": status,
                    },
                )
            )
        return out

    async def attribute_floors(self, collection: str) -> dict[str, dict[str, Decimal]]:
        """Минимальные цены по атрибутам коллекции.

        Portals считает floor отдельно для каждой модели, символа и
        фона. Это качественно меняет оценку: подарок с редкой моделью
        стоит кратно дороже floor коллекции, и именно там возникают
        недооценённые лоты. Без этих данных пришлось бы неделями
        копить собственную историю продаж.

        Эндпоинт отдаёт значения только с токеном; без него вернётся
        пустой словарь, и оценка откатится на историю.

        Returns:
            {"models": {название: floor}, "symbols": {...}, "backdrops": {...}}
        """
        cached = self._floor_cache.get(collection)
        if cached and (dt.datetime.utcnow() - cached[0]).total_seconds() < 600:
            return cached[1]

        short_name = short_collection_name(collection)
        try:
            data = await self.request(
                "GET", "/collections/filters", params={"short_names": short_name}
            )
        except Exception as exc:  # noqa: BLE001 - оценка обойдётся без этого
            log.debug("Portals: floor по атрибутам недоступен: %s", exc)
            return {}

        # В ответе ключом стоит короткое имя, которое мы и спрашивали.
        out = self._parse_attribute_floors(data, short_name)
        self._floor_cache[collection] = (dt.datetime.utcnow(), out)
        if not any(out.values()):
            log.info(
                "Portals: floor по атрибутам для %r пуст — вероятно, нужен токен",
                collection,
            )
        return out

    @staticmethod
    def _parse_attribute_floors(
        data: object, collection: str
    ) -> dict[str, dict[str, Decimal]]:
        """Разобрать ответ, не полагаясь на одну форму.

        Площадка возвращала разные структуры, поэтому поддерживаются
        обе: словарь floor_prices и список атрибутов внутри collections.
        """
        out: dict[str, dict[str, Decimal]] = {
            "models": {},
            "symbols": {},
            "backdrops": {},
        }
        if not isinstance(data, dict):
            return out

        # Форма 1: floor_prices[коллекция][раздел][название] = цена
        block = (data.get("floor_prices") or {}).get(collection)
        if isinstance(block, dict):
            for section in out:
                values = block.get(section)
                if isinstance(values, dict):
                    for name, price in values.items():
                        value = to_decimal(price)
                        if value and value > 0:
                            out[section][str(name)] = value

        # Форма 2: collections[коллекция][раздел] = [{name, floor_price}]
        block = (data.get("collections") or {}).get(collection)
        if isinstance(block, dict):
            for section in out:
                values = block.get(section)
                if not isinstance(values, list):
                    continue
                for entry in values:
                    if not isinstance(entry, dict):
                        continue
                    name = first(entry, "name", "value", "title")
                    price = to_decimal(
                        first(entry, "floor_price", "floor", "price", "min_price")
                    )
                    if name and price and price > 0:
                        out[section][str(name)] = price
        return out

    async def fetch_listing(self, external_id: str) -> ListingDTO | None:
        """Перечитать конкретный лот перед покупкой.

        Берётся прямой эндпоинт ``GET /nfts/{id}``. Прежде здесь стоял
        поиск с ``query=<id>``, но это текстовый поиск: он игнорирует
        идентификатор и возвращает посторонние лоты. Совпадение не
        находилось никогда, и каждая покупка на Portals отбивалась
        сообщением «лот больше не выставлен».
        """
        if not external_id:
            return None
        try:
            data = await self.request("GET", f"/nfts/{external_id}")
        except Exception as exc:  # noqa: BLE001 - отсутствие лота не ошибка
            log.debug("Portals: лот %s перечитать не удалось: %s", external_id, exc)
            return None

        # Эндпоинт отдаёт один объект, а не список.
        rows = data if isinstance(data, list) else [data]
        for listing in self._to_listings(rows):
            if listing.external_id == external_id:
                return listing
        return None

    # ------------------------------------------------------------------
    # Боевые операции
    # ------------------------------------------------------------------
    async def buy(
        self, *, external_id: str, expected_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Купить лот по точной ожидаемой цене.

        Лот перечитывается заново: если он исчез или цена изменилась,
        платёжный запрос не отправляется вовсе.
        """
        self._require(Capability.BUY)

        fresh = await self.fetch_listing(external_id)
        if fresh is None:
            return ExecutionResult(ok=False, detail="лот больше не выставлен")
        if fresh.price != expected_price:
            return ExecutionResult(
                ok=False,
                detail=(
                    f"цена изменилась: ожидали {expected_price} TON, "
                    f"на площадке {fresh.price} TON"
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
        """Изменить цену своего лота."""
        self._require(Capability.REPRICE)
        return await self.execute_contract_op(
            "reprice", external_id=external_id, price=new_price
        )

    async def cancel(self, *, external_id: str, idempotency_key: str) -> ExecutionResult:
        """Снять лот с продажи.

        Отдельного эндпоинта снятия у площадки нет; операция доступна,
        только если описана в markets/portals.json.
        """
        self._require(Capability.CANCEL)
        return await self.execute_contract_op("cancel", external_id=external_id)

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
