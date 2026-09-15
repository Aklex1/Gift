"""Адаптер официального resale-маркета подарков Telegram (MTProto).

Единственная площадка со статусом SUPPORTED: используется официальный
пользовательский API, документированный на core.telegram.org/api/gifts.
Только ей разрешён режим AUTO.

Покрываемые вызовы:
    payments.getStarGifts              — каталог коллекций и floor
    payments.getResaleStarGifts        — активные лоты перепродажи
    payments.getUniqueStarGiftValueInfo— floor/average/last sale подарка
    payments.getUniqueStarGift         — состояние конкретного подарка
    payments.getSavedStarGifts         — собственный инвентарь
    payments.getStarsStatus            — баланс Stars
    payments.getStarsTransactions      — история Stars
    payments.getPaymentForm + sendStarsForm — покупка лота
    payments.updateStarGiftPrice       — выставить/изменить/снять цену
    payments.transferStarGift          — передача подарка
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

from app.adapters.base import (
    AdapterError,
    BalanceDTO,
    Capability,
    CapabilityStatus,
    ExecutionResult,
    GiftRef,
    ListingDTO,
    MarketAdapter,
    OutcomeUnknown,
    SaleDTO,
)
from app.adapters.telegram_gateway import TelegramGateway, default_gateway
from app.enums import Currency, Market

log = logging.getLogger(__name__)

#: 1 TON = 10^9 nanoton.
NANO = Decimal("1000000000")


def _amount_to_money(obj: Any) -> tuple[Decimal, Currency]:
    """Привести StarsAmount / StarsTonAmount к (сумма, валюта).

    StarsAmount несёт целые Stars + нанодоли; StarsTonAmount — нанотоны.
    """
    cls = type(obj).__name__
    if cls == "StarsTonAmount":
        return (Decimal(getattr(obj, "amount", 0)) / NANO, Currency.TON)
    amount = Decimal(getattr(obj, "amount", 0))
    nanos = Decimal(getattr(obj, "nanos", 0) or 0)
    return (amount + nanos / NANO, Currency.STARS)


def _pick_price(resell_amount: Any) -> tuple[Decimal, Currency] | None:
    """Выбрать цену лота, предпочитая Stars.

    ``resell_amount`` — список вариантов оплаты (Stars и/или TON).
    """
    if not resell_amount:
        return None
    variants = [_amount_to_money(a) for a in resell_amount]
    for value, currency in variants:
        if currency is Currency.STARS and value > 0:
            return (value, currency)
    for value, currency in variants:
        if value > 0:
            return (value, currency)
    return None


def _rarity(attr: Any) -> float | None:
    """Редкость атрибута в долях (permille/1000)."""
    rarity = getattr(attr, "rarity", None)
    permille = getattr(rarity, "permille", None)
    if permille is None:
        return None
    return round(float(permille) / 1000.0, 6)


def _gift_ref(gift: Any) -> GiftRef:
    """Собрать каноническую идентичность из StarGiftUnique."""
    model = backdrop = symbol = None
    model_r = backdrop_r = symbol_r = None

    for attr in getattr(gift, "attributes", None) or []:
        name = type(attr).__name__
        if name == "StarGiftAttributeModel":
            model, model_r = getattr(attr, "name", None), _rarity(attr)
        elif name == "StarGiftAttributeBackdrop":
            backdrop, backdrop_r = getattr(attr, "name", None), _rarity(attr)
        elif name == "StarGiftAttributePattern":
            symbol, symbol_r = getattr(attr, "name", None), _rarity(attr)

    return GiftRef(
        collection=getattr(gift, "title", None) or "unknown",
        number=getattr(gift, "num", None),
        slug=getattr(gift, "slug", None),
        model=model,
        backdrop=backdrop,
        symbol=symbol,
        tg_gift_id=getattr(gift, "gift_id", None) or getattr(gift, "id", None),
        nft_address=getattr(gift, "gift_address", None),
        attributes={
            "model_rarity": model_r,
            "backdrop_rarity": backdrop_r,
            "symbol_rarity": symbol_r,
            "availability_issued": getattr(gift, "availability_issued", None),
            "availability_total": getattr(gift, "availability_total", None),
            "owner_name": getattr(gift, "owner_name", None),
            "resale_ton_only": bool(getattr(gift, "resale_ton_only", False)),
            "value_amount": getattr(gift, "value_amount", None),
            "value_currency": getattr(gift, "value_currency", None),
        },
    )


class TelegramAdapter(MarketAdapter):
    """Официальный маркет перепродажи подарков Telegram."""

    market = Market.TELEGRAM
    native_currency = Currency.STARS

    capabilities = {
        Capability.SEARCH: CapabilityStatus.SUPPORTED,
        Capability.HISTORY: CapabilityStatus.SUPPORTED,
        Capability.BALANCE: CapabilityStatus.SUPPORTED,
        Capability.INVENTORY: CapabilityStatus.SUPPORTED,
        Capability.BUY: CapabilityStatus.SUPPORTED,
        Capability.LIST: CapabilityStatus.SUPPORTED,
        Capability.REPRICE: CapabilityStatus.SUPPORTED,
        Capability.CANCEL: CapabilityStatus.SUPPORTED,
        Capability.RECONCILE: CapabilityStatus.SUPPORTED,
        Capability.TRANSFER: CapabilityStatus.SUPPORTED,
    }

    def __init__(self, gateway: TelegramGateway | None = None) -> None:
        """Адаптер конкретного торгового аккаунта.

        Args:
            gateway: шлюз аккаунта. Без него берётся аккаунт по
                умолчанию — первый активный в таблице.
        """
        self._gateway = gateway
        #: Кэш каталога коллекций: title(lower) -> gift_id.
        self._catalog: dict[str, int] = {}
        self._catalog_at: dt.datetime | None = None
        #: floor по коллекции из каталога: gift_id -> resell_min_stars.
        self._floors: dict[int, Decimal] = {}

    @property
    def gateway(self) -> TelegramGateway:
        """Шлюз, через который идут все вызовы этого адаптера."""
        if self._gateway is None:
            self._gateway = default_gateway()
        return self._gateway

    @property
    def account_id(self) -> int | None:
        """Аккаунт, которому принадлежит адаптер."""
        return self.gateway.account_id

    @property
    def label(self) -> str:
        """Имя аккаунта для сообщений."""
        return self.gateway.label

    # ------------------------------------------------------------------
    # Каталог
    # ------------------------------------------------------------------
    async def catalog(self, *, force: bool = False) -> dict[str, int]:
        """Каталог коллекций подарков: название -> gift_id.

        Нужен, потому что поиск по resale идёт строго по gift_id коллекции.
        """
        fresh = (
            self._catalog_at
            and (dt.datetime.utcnow() - self._catalog_at).total_seconds() < 3600
        )
        if self._catalog and fresh and not force:
            return self._catalog

        from telethon.tl import functions

        res = await self.gateway.call(functions.payments.GetStarGiftsRequest(hash=0))
        catalog: dict[str, int] = {}
        floors: dict[int, Decimal] = {}
        for gift in getattr(res, "gifts", None) or []:
            gift_id = getattr(gift, "id", None)
            title = getattr(gift, "title", None)
            if gift_id is None:
                continue
            if title:
                catalog[title.strip().lower()] = gift_id
            resell_min = getattr(gift, "resell_min_stars", None)
            if resell_min:
                floors[gift_id] = Decimal(resell_min)
        self._catalog = catalog
        self._floors = floors
        self._catalog_at = dt.datetime.utcnow()
        log.info("Каталог подарков Telegram: %s коллекций", len(catalog))
        return catalog

    async def resolve_collection(self, name: str) -> int | None:
        """Найти gift_id коллекции по названию (регистронезависимо)."""
        catalog = await self.catalog()
        key = name.strip().lower()
        if key in catalog:
            return catalog[key]
        for title, gift_id in catalog.items():
            if key in title:
                return gift_id
        return None

    def floor_of(self, gift_id: int) -> Decimal | None:
        """Минимальная цена перепродажи коллекции из каталога, в Stars."""
        return self._floors.get(gift_id)

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
        """Активные лоты перепродажи, отсортированные по цене.

        Без указания коллекции сканируются коллекции, где вообще есть
        предложения перепродажи (availability_resale > 0).
        """
        self._require(Capability.SEARCH)
        from telethon.tl import functions

        targets: list[int] = []
        if collection:
            gift_id = await self.resolve_collection(collection)
            if gift_id is None:
                # Пустой ответ здесь выглядел бы как «на рынке ничего
                # нет», хотя дело в названии из настроек стратегии.
                from app.adapters.base import SearchSkipped

                raise SearchSkipped(
                    f"коллекция {collection!r} не найдена в каталоге Telegram — "
                    f"проверьте название в стратегии"
                )
            targets = [gift_id]
        else:
            await self.catalog()
            targets = list(self._floors.keys())[:20]

        out: list[ListingDTO] = []
        per_target = max(10, limit // max(1, len(targets)))

        for gift_id in targets:
            offset = ""
            fetched = 0
            while fetched < per_target:
                res = await self.gateway.call(
                    functions.payments.GetResaleStarGiftsRequest(
                        gift_id=gift_id,
                        offset=offset,
                        limit=min(50, per_target - fetched),
                        sort_by_price=True,
                    )
                )
                gifts = getattr(res, "gifts", None) or []
                if not gifts:
                    break
                for gift in gifts:
                    price = _pick_price(getattr(gift, "resell_amount", None))
                    slug = getattr(gift, "slug", None)
                    if price is None or not slug:
                        continue
                    value, currency = price
                    if max_price is not None and currency is Currency.STARS and value > max_price:
                        # Лоты отсортированы по возрастанию цены — дальше только дороже.
                        fetched = per_target
                        break
                    ref = _gift_ref(gift)
                    if model and (ref.model or "").lower() != model.lower():
                        continue
                    if backdrop and (ref.backdrop or "").lower() != backdrop.lower():
                        continue
                    if symbol and (ref.symbol or "").lower() != symbol.lower():
                        continue
                    out.append(
                        ListingDTO(
                            market=self.market,
                            external_id=slug,
                            gift=ref,
                            price=value,
                            currency=currency,
                            seller=getattr(gift, "owner_name", None),
                            raw={"gift_id": gift_id, "num": getattr(gift, "num", None)},
                        )
                    )
                    fetched += 1
                    if len(out) >= limit:
                        return out
                offset = getattr(res, "next_offset", None) or ""
                if not offset:
                    break
        return out

    async def value_info(self, slug: str) -> dict[str, Any]:
        """Официальная оценка подарка: floor, average, последняя продажа.

        Это самый качественный источник данных для valuation — цифры
        приходят от самого Telegram, а не из нашей выборки.
        """
        from telethon.tl import functions

        res = await self.gateway.call(
            functions.payments.GetUniqueStarGiftValueInfoRequest(slug=slug)
        )
        currency = (getattr(res, "currency", None) or "XTR").upper()
        # Значения приходят в минимальных единицах указанной валюты.
        divisor = Decimal(100) if currency not in {"XTR", "TON"} else Decimal(1)

        def conv(v: Any) -> Decimal | None:
            return None if v is None else Decimal(v) / divisor

        return {
            "currency": Currency.STARS if currency == "XTR" else Currency.TON,
            "value": conv(getattr(res, "value", None)),
            "floor_price": conv(getattr(res, "floor_price", None)),
            "average_price": conv(getattr(res, "average_price", None)),
            "last_sale_price": conv(getattr(res, "last_sale_price", None)),
            "last_sale_date": getattr(res, "last_sale_date", None),
            "listed_count": getattr(res, "listed_count", None),
            "value_is_average": bool(getattr(res, "value_is_average", False)),
            "initial_sale_price": conv(getattr(res, "initial_sale_price", None)),
        }

    async def history(
        self, *, collection: str | None = None, model: str | None = None, limit: int = 200
    ) -> list[SaleDTO]:
        """История собственных Stars-операций.

        Важное ограничение из аудита ТЗ: глобальной истории продаж чужих
        подарков MTProto не отдаёт. Здесь — только наши транзакции;
        рыночная оценка строится на ``value_info`` и floor каталога.
        """
        self._require(Capability.HISTORY)
        from telethon.tl import functions, types

        res = await self.gateway.call(
            functions.payments.GetStarsTransactionsRequest(
                peer=types.InputPeerSelf(), offset="", limit=min(limit, 100)
            )
        )
        out: list[SaleDTO] = []
        for tx in getattr(res, "history", None) or []:
            amount, currency = _amount_to_money(getattr(tx, "amount", None))
            if amount == 0:
                continue
            out.append(
                SaleDTO(
                    market=self.market,
                    external_id=str(getattr(tx, "id", "") or ""),
                    gift=GiftRef(collection=collection or "self"),
                    price=abs(amount),
                    currency=currency,
                    happened_at=getattr(tx, "date", None) or dt.datetime.utcnow(),
                    raw={"title": getattr(tx, "title", None)},
                )
            )
        return out

    async def balance(self) -> list[BalanceDTO]:
        """Баланс Stars и TON торгового аккаунта."""
        self._require(Capability.BALANCE)
        from telethon.tl import functions, types

        out: list[BalanceDTO] = []
        res = await self.gateway.call(
            functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf())
        )
        amount, currency = _amount_to_money(getattr(res, "balance", None))
        out.append(BalanceDTO(market=self.market, currency=currency, amount=amount))

        try:
            res_ton = await self.gateway.call(
                functions.payments.GetStarsStatusRequest(
                    peer=types.InputPeerSelf(), ton=True
                )
            )
            ton_amount, ton_currency = _amount_to_money(getattr(res_ton, "balance", None))
            out.append(
                BalanceDTO(market=self.market, currency=ton_currency, amount=ton_amount)
            )
        except Exception as exc:  # noqa: BLE001 - TON-баланс необязателен
            log.debug("TON-баланс недоступен: %s", exc)
        return out

    async def inventory(self) -> list[ListingDTO]:
        """Собственные подарки с ценой, если они выставлены.

        В ``raw`` кладём msg_id/saved_id и таймстемпы cooldown — без них
        нельзя корректно выставить подарок на продажу или передать.
        """
        self._require(Capability.INVENTORY)
        from telethon.tl import functions, types

        out: list[ListingDTO] = []
        offset = ""
        for _ in range(10):  # не более 10 страниц за проход
            res = await self.gateway.call(
                functions.payments.GetSavedStarGiftsRequest(
                    peer=types.InputPeerSelf(),
                    offset=offset,
                    limit=100,
                    exclude_unique=False,
                )
            )
            saved_items = getattr(res, "gifts", None) or []
            if not saved_items:
                break
            for saved in saved_items:
                gift = getattr(saved, "gift", None)
                if gift is None or type(gift).__name__ != "StarGiftUnique":
                    continue
                ref = _gift_ref(gift)
                price = _pick_price(getattr(gift, "resell_amount", None))
                value, currency = price if price else (Decimal(0), Currency.STARS)
                out.append(
                    ListingDTO(
                        market=self.market,
                        external_id=ref.slug or str(getattr(gift, "id", "")),
                        gift=ref,
                        price=value,
                        currency=currency,
                        raw={
                            "msg_id": getattr(saved, "msg_id", None),
                            "saved_id": getattr(saved, "saved_id", None),
                            "can_resell_at": getattr(saved, "can_resell_at", None),
                            "can_transfer_at": getattr(saved, "can_transfer_at", None),
                            "can_export_at": getattr(saved, "can_export_at", None),
                            "transfer_stars": getattr(saved, "transfer_stars", None),
                            "is_listed": bool(price and value > 0),
                        },
                    )
                )
            offset = getattr(res, "next_offset", None) or ""
            if not offset:
                break
        return out

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------
    def _saved_ref(self, *, slug: str | None, msg_id: int | None) -> Any:
        """Построить InputSavedStarGift для операций над своим подарком."""
        from telethon.tl import types

        if msg_id:
            return types.InputSavedStarGiftUser(msg_id=msg_id)
        if slug:
            return types.InputSavedStarGiftSlug(slug=slug)
        raise AdapterError("Не указан ни slug, ни msg_id подарка")

    async def buy(
        self, *, external_id: str, expected_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Купить лот перепродажи по точной ожидаемой цене.

        Порядок строгий:
        1. Заново прочитать лот (fresh lookup).
        2. Сверить цену с ожидаемой — при расхождении отказ без покупки.
        3. Получить payment form и ещё раз сверить сумму в инвойсе.
        4. Отправить форму.

        Обрыв связи на шаге 4 даёт OutcomeUnknown: повторять нельзя,
        исход сводит reconcile.
        """
        self._require(Capability.BUY)
        from telethon.tl import functions, types

        # --- 1-2. Свежее чтение и точная сверка цены ---
        fresh = await self.gateway.call(
            functions.payments.GetUniqueStarGiftRequest(slug=external_id)
        )
        gift = getattr(fresh, "gift", None)
        if gift is None:
            return ExecutionResult(ok=False, detail="Лот больше не существует")

        price = _pick_price(getattr(gift, "resell_amount", None))
        if price is None:
            return ExecutionResult(ok=False, detail="Лот снят с продажи")
        actual_price, currency = price
        if actual_price != expected_price:
            return ExecutionResult(
                ok=False,
                detail=(
                    f"Цена изменилась: ожидали {expected_price}, "
                    f"на площадке {actual_price} {currency.value}"
                ),
            )
        if currency is not Currency.STARS:
            return ExecutionResult(
                ok=False,
                detail=f"Лот продаётся только за {currency.value}; оплата Stars недоступна",
            )

        # --- 3. Payment form ---
        invoice = types.InputInvoiceStarGiftResale(
            slug=external_id, to_id=types.InputPeerSelf()
        )
        form = await self.gateway.call(
            functions.payments.GetPaymentFormRequest(invoice=invoice)
        )
        form_id = getattr(form, "form_id", None)
        if form_id is None:
            return ExecutionResult(ok=False, detail="Telegram не выдал форму оплаты")

        form_total = Decimal(
            sum(
                getattr(p, "amount", 0)
                for p in getattr(getattr(form, "invoice", None), "prices", None) or []
            )
        )
        if form_total and form_total != expected_price:
            return ExecutionResult(
                ok=False,
                detail=f"Сумма в форме оплаты {form_total} != ожидаемой {expected_price}",
            )

        # --- 4. Отправка формы (реальное списание Stars) ---
        try:
            result = await self.gateway.call(
                functions.payments.SendStarsFormRequest(form_id=form_id, invoice=invoice),
                write=True,
            )
        except OutcomeUnknown as exc:
            exc.external_ref = external_id
            raise

        ok = type(result).__name__ in {"PaymentResult", "PaymentVerificationNeeded"}
        return ExecutionResult(
            ok=ok if ok else None,
            external_ref=external_id,
            executed_price=actual_price,
            currency=Currency.STARS,
            detail=type(result).__name__,
        )

    async def list_for_sale(
        self, *, gift_ref: GiftRef, price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Выставить собственный подарок на перепродажу за Stars."""
        self._require(Capability.LIST)
        return await self._set_price(gift_ref=gift_ref, price=price)

    async def reprice(
        self, *, external_id: str, new_price: Decimal, idempotency_key: str
    ) -> ExecutionResult:
        """Изменить цену своего лота."""
        self._require(Capability.REPRICE)
        return await self._set_price(
            gift_ref=GiftRef(collection="", slug=external_id), price=new_price
        )

    async def cancel(self, *, external_id: str, idempotency_key: str) -> ExecutionResult:
        """Снять лот с продажи (цена 0 = снятие)."""
        self._require(Capability.CANCEL)
        return await self._set_price(
            gift_ref=GiftRef(collection="", slug=external_id), price=Decimal(0)
        )

    async def _set_price(self, *, gift_ref: GiftRef, price: Decimal) -> ExecutionResult:
        """Общая реализация list/reprice/cancel через updateStarGiftPrice."""
        from telethon.tl import functions, types

        stargift = self._saved_ref(
            slug=gift_ref.slug, msg_id=gift_ref.attributes.get("msg_id")
        )
        whole = int(price)
        nanos = int((price - Decimal(whole)) * NANO)
        amount = types.StarsAmount(amount=whole, nanos=nanos)

        try:
            await self.gateway.call(
                functions.payments.UpdateStarGiftPriceRequest(
                    stargift=stargift, resell_amount=amount
                ),
                write=True,
            )
        except OutcomeUnknown as exc:
            exc.external_ref = gift_ref.slug
            raise

        return ExecutionResult(
            ok=True,
            external_ref=gift_ref.slug,
            executed_price=price,
            currency=Currency.STARS,
            detail="цена обновлена" if price > 0 else "снято с продажи",
        )

    async def reconcile(
        self, *, external_ref: str | None, gift_ref: GiftRef | None
    ) -> ExecutionResult:
        """Свести исход: проверить, кому сейчас принадлежит подарок.

        Это и есть замена слепому retry: читаем реальное состояние
        и делаем вывод, прошла операция или нет.
        """
        self._require(Capability.RECONCILE)
        from telethon.tl import functions

        slug = external_ref or (gift_ref.slug if gift_ref else None)
        if not slug:
            return ExecutionResult(ok=None, detail="Нечего сверять: нет slug")

        me = await self.gateway.me()
        my_id = getattr(me, "id", None)

        res = await self.gateway.call(functions.payments.GetUniqueStarGiftRequest(slug=slug))
        gift = getattr(res, "gift", None)
        if gift is None:
            return ExecutionResult(ok=False, external_ref=slug, detail="Подарок не найден")

        owner = getattr(gift, "owner_id", None)
        owner_id = getattr(owner, "user_id", None) or getattr(owner, "channel_id", None)
        mine = owner_id is not None and my_id is not None and owner_id == my_id

        price = _pick_price(getattr(gift, "resell_amount", None))
        return ExecutionResult(
            ok=mine,
            external_ref=slug,
            executed_price=price[0] if price else None,
            currency=price[1] if price else None,
            detail="подарок принадлежит нам" if mine else "подарок принадлежит другому",
            raw={"owner_id": owner_id, "listed": bool(price)},
        )

    async def transfer(self, *, gift_ref: GiftRef, to_username: str) -> ExecutionResult:
        """Передать подарок другому аккаунту."""
        self._require(Capability.TRANSFER)
        from telethon.tl import functions

        client = await self.gateway.client()
        peer = await client.get_input_entity(to_username)
        stargift = self._saved_ref(
            slug=gift_ref.slug, msg_id=gift_ref.attributes.get("msg_id")
        )
        await self.gateway.call(
            functions.payments.TransferStarGiftRequest(stargift=stargift, to_id=peer),
            write=True,
        )
        return ExecutionResult(ok=True, external_ref=gift_ref.slug, detail="передан")

    async def close(self) -> None:
        """Соединение живёт в шлюзе и закрывается вместе с ним."""
