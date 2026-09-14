"""Канонический домен и схема БД.

Соответствует разделу 6 ТЗ: Gift, listing, currency, FX snapshot,
fee schedule, strategy, budget, intent, transaction, position,
market fact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator

from app.enums import (
    Capability,
    CapabilityStatus,
    Confidence,
    Currency,
    IntentKind,
    IntentStatus,
    Market,
    PositionStatus,
    ReservationStatus,
    TradeMode,
)


class EnumStr(TypeDecorator):
    """Хранит перечисление строкой, но всегда отдаёт объект enum.

    Без этого значение, прочитанное из БД, оставалось обычной строкой,
    и сравнение вида ``intent.status is IntentStatus.CONFIRMED`` молча
    давало False. Декоратор убирает целый класс таких ошибок.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type, length: int = 32) -> None:
        self.enum_cls = enum_cls
        super().__init__(length)

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        """Enum -> строка при записи."""
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value.value
        return str(value)

    def process_result_value(self, value: object, dialect: object) -> object:
        """Строка -> enum при чтении."""
        if value is None:
            return None
        return self.enum_cls(value)


def utcnow() -> dt.datetime:
    """Текущее время в UTC (naive, как хранится в БД)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Базовый класс моделей."""

    type_annotation_map = {dict: JSON, list: JSON}


#: Денежная сумма: 28 знаков, 9 после запятой — хватает и на nanoTON.
Money = Numeric(28, 9)


class TimestampMixin:
    """Общие поля времени создания и обновления."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# ----------------------------------------------------------------------
# Идентичность подарка
# ----------------------------------------------------------------------
class Gift(Base, TimestampMixin):
    """Канонический подарок.

    Один и тот же подарок на разных площадках должен схлопываться
    в одну запись — это условие кросс-рыночного арбитража.
    """

    __tablename__ = "gifts"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Стабильный ключ идентичности: collection#number либо slug.
    canonical_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    collection: Mapped[str] = mapped_column(String(128), index=True)
    number: Mapped[int | None] = mapped_column(Integer, index=True)
    slug: Mapped[str | None] = mapped_column(String(255), index=True)

    # Атрибуты, определяющие редкость и цену.
    model: Mapped[str | None] = mapped_column(String(128), index=True)
    backdrop: Mapped[str | None] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(128), index=True)
    model_rarity: Mapped[float | None] = mapped_column(Numeric(10, 4))
    backdrop_rarity: Mapped[float | None] = mapped_column(Numeric(10, 4))
    symbol_rarity: Mapped[float | None] = mapped_column(Numeric(10, 4))

    #: Telegram-идентификаторы (msg_id подарка в MTProto).
    tg_gift_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    #: NFT-адрес в TON, если подарок выведен on-chain.
    nft_address: Mapped[str | None] = mapped_column(String(128), index=True)

    attributes: Mapped[dict] = mapped_column(JSON, default=dict)

    listings: Mapped[list["Listing"]] = relationship(back_populates="gift")
    positions: Mapped[list["Position"]] = relationship(back_populates="gift")

    __table_args__ = (
        Index("ix_gifts_traits", "collection", "model", "backdrop", "symbol"),
    )

    def __repr__(self) -> str:
        return f"<Gift {self.canonical_key}>"


# ----------------------------------------------------------------------
# Рыночные данные
# ----------------------------------------------------------------------
class Listing(Base, TimestampMixin):
    """Активный лот на площадке (снимок на момент скана)."""

    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    gift_id: Mapped[int] = mapped_column(ForeignKey("gifts.id"), index=True)
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)

    #: Идентификатор лота на стороне площадки.
    external_id: Mapped[str] = mapped_column(String(128), index=True)

    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency))

    #: Цена, приведённая к Stars по FX-снапшоту — для сравнения площадок.
    price_stars: Mapped[Decimal | None] = mapped_column(Money, index=True)

    seller: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    gift: Mapped[Gift] = relationship(back_populates="listings")

    __table_args__ = (
        UniqueConstraint("market", "external_id", name="uq_listing_market_ext"),
        Index("ix_listings_active_price", "market", "is_active", "price_stars"),
    )


class MarketFact(Base):
    """Факт состоявшейся сделки — основа для оценки и ликвидности."""

    __tablename__ = "market_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    gift_id: Mapped[int | None] = mapped_column(ForeignKey("gifts.id"), index=True)
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)

    collection: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str | None] = mapped_column(String(128), index=True)
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))

    price: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency))
    price_stars: Mapped[Decimal | None] = mapped_column(Money, index=True)

    happened_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    #: Флаг подозрения на wash trade (продавец = покупатель и т. п.).
    suspected_wash: Mapped[bool] = mapped_column(Boolean, default=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_facts_lookup", "collection", "model", "happened_at"),
        UniqueConstraint("market", "external_id", name="uq_fact_market_ext"),
    )


class FxSnapshot(Base):
    """Курс валют с таймстемпом.

    ТЗ прямо требует timestamped FX snapshot: без него кросс-рыночная
    прибыль считается неверно.
    """

    __tablename__ = "fx_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[Currency] = mapped_column(EnumStr(Currency), index=True)
    quote: Mapped[Currency] = mapped_column(EnumStr(Currency), index=True)
    rate: Mapped[Decimal] = mapped_column(Money)
    source: Mapped[str] = mapped_column(String(64))
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)

    __table_args__ = (Index("ix_fx_pair_time", "base", "quote", "taken_at"),)


class FeeSchedule(Base, TimestampMixin):
    """Версионированная матрица комиссий площадки.

    ROI считается только с учётом актуальной версии.
    """

    __tablename__ = "fee_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    #: Доля комиссии площадки с продавца, 0.05 = 5%.
    sale_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    #: Доля комиссии при покупке.
    buy_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    #: Роялти автора коллекции.
    royalty: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    #: Фиксированная сетевая комиссия за операцию.
    network_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency), default=Currency.STARS)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("market", "version", name="uq_fee_market_version"),
    )


# ----------------------------------------------------------------------
# Возможности адаптеров
# ----------------------------------------------------------------------
class AdapterCapability(Base, TimestampMixin):
    """Матрица возможностей площадки — контракт из раздела 6 ТЗ."""

    __tablename__ = "adapter_capabilities"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)
    capability: Mapped[Capability] = mapped_column(EnumStr(Capability))
    status: Mapped[CapabilityStatus] = mapped_column(
        EnumStr(CapabilityStatus), default=CapabilityStatus.UNAVAILABLE
    )
    #: Результат последней живой проверки (probe).
    last_probe_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_probe_ok: Mapped[bool | None] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("market", "capability", name="uq_cap_market_cap"),
    )


# ----------------------------------------------------------------------
# Бюджет
# ----------------------------------------------------------------------
class Budget(Base, TimestampMixin):
    """Кошелёк стратегии с жёстким потолком.

    Инвариант: reserved + spent <= hard_cap. Проверяется под блокировкой
    строки в той же транзакции, что и создание резерва.
    """

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency), default=Currency.STARS)

    hard_cap: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    reserved: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    spent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    #: Потрачено за текущие сутки (сбрасывается по daily_reset_at).
    daily_spent: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    daily_limit: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    daily_reset_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="budget")

    @property
    def available(self) -> Decimal:
        """Свободные средства с учётом активных резервов."""
        return Decimal(self.hard_cap) - Decimal(self.reserved) - Decimal(self.spent)

    @property
    def daily_available(self) -> Decimal:
        """Остаток суточного лимита. 0 в лимите = лимит не задан."""
        if Decimal(self.daily_limit) <= 0:
            return Decimal("999999999")
        return Decimal(self.daily_limit) - Decimal(self.daily_spent)


class Reservation(Base, TimestampMixin):
    """Атомарный резерв средств под конкретное намерение.

    Создаётся ДО внешнего вызова. Это защита от overspend при
    параллельных стратегиях.
    """

    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), index=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("intents.id"), index=True)

    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency), default=Currency.STARS)
    status: Mapped[ReservationStatus] = mapped_column(
        EnumStr(ReservationStatus), default=ReservationStatus.ACTIVE, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    #: Фактически списано при расчёте (может быть меньше резерва).
    settled_amount: Mapped[Decimal | None] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(Text)

    budget: Mapped[Budget] = relationship(back_populates="reservations")


# ----------------------------------------------------------------------
# Стратегии
# ----------------------------------------------------------------------
class Strategy(Base, TimestampMixin):
    """Независимая торговая стратегия со своим бюджетом и фильтрами."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    #: Режим исполнения именно этой стратегии.
    mode: Mapped[TradeMode] = mapped_column(EnumStr(TradeMode), default=TradeMode.SAFE)
    #: Приоритет при конкуренции за общий бюджет: больше = важнее.
    priority: Mapped[int] = mapped_column(Integer, default=100)

    budget_id: Mapped[int | None] = mapped_column(ForeignKey("budgets.id"))

    #: Площадки, на которых стратегия ищет лоты.
    markets: Mapped[list] = mapped_column(JSON, default=list)

    # --- фильтры отбора ---
    collections: Mapped[list] = mapped_column(JSON, default=list)
    models: Mapped[list] = mapped_column(JSON, default=list)
    backdrops: Mapped[list] = mapped_column(JSON, default=list)
    symbols: Mapped[list] = mapped_column(JSON, default=list)

    min_price_stars: Mapped[Decimal | None] = mapped_column(Money)
    max_price_stars: Mapped[Decimal | None] = mapped_column(Money)

    #: Минимальный чистый ROI после комиссий. В ТЗ была опечатка "Max ROI" —
    #: логика требует именно минимума, см. раздел 10 аудита.
    min_roi: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.15"))
    #: Максимально допустимый risk score 0..100.
    max_risk: Mapped[int] = mapped_column(Integer, default=60)
    #: Минимальное качество выборки рыночных данных.
    min_confidence: Mapped[Confidence] = mapped_column(
        EnumStr(Confidence), default=Confidence.MEDIUM
    )

    #: Наценка при выставлении на продажу, доля от оценки.
    sell_markup: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.25"))
    #: Шаг снижения цены при репрайсинге, доля.
    reprice_step: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.05"))
    #: Пауза между снижениями цены, часов.
    reprice_cooldown_h: Mapped[int] = mapped_column(Integer, default=12)
    #: Ниже этой доли от цены покупки не опускаться.
    floor_ratio: Mapped[Decimal] = mapped_column(Money, default=Decimal("1.02"))

    max_open_positions: Mapped[int] = mapped_column(Integer, default=5)

    budget: Mapped[Budget | None] = relationship()


# ----------------------------------------------------------------------
# Исполнение
# ----------------------------------------------------------------------
class Intent(Base, TimestampMixin):
    """Торговое намерение — единица execution saga.

    Каждый внешний write-вызов проходит через Intent: это даёт
    идемпотентность и корректную обработку UNKNOWN.
    """

    __tablename__ = "intents"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: Ключ идемпотентности: повторная попытка с тем же ключом не создаёт
    #: второй покупки.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    kind: Mapped[IntentKind] = mapped_column(EnumStr(IntentKind), index=True)
    status: Mapped[IntentStatus] = mapped_column(
        EnumStr(IntentStatus), default=IntentStatus.PLANNED, index=True
    )
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)
    mode: Mapped[TradeMode] = mapped_column(EnumStr(TradeMode), default=TradeMode.SEMI)

    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"), index=True)
    gift_id: Mapped[int | None] = mapped_column(ForeignKey("gifts.id"), index=True)
    listing_external_id: Mapped[str | None] = mapped_column(String(128))

    #: Цена, на которую рассчитывали при планировании.
    planned_price: Mapped[Decimal | None] = mapped_column(Money)
    #: Цена, по которой сделка реально прошла.
    executed_price: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency), default=Currency.STARS)

    #: Снимок решения: ROI, risk, confidence, обоснование.
    decision: Mapped[dict] = mapped_column(JSON, default=dict)

    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    settled_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    #: Сколько раз пытались свести исход. Слепой retry запрещён.
    reconcile_attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (Index("ix_intents_status_kind", "status", "kind"),)


class Transaction(Base):
    """Неизменяемая запись о движении средств — журнал для PnL."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("intents.id"), index=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)

    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)
    #: buy | sell | fee | deposit | withdrawal
    kind: Mapped[str] = mapped_column(String(32), index=True)

    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[Currency] = mapped_column(EnumStr(Currency))
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))

    happened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    external_ref: Mapped[str | None] = mapped_column(String(255), index=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class Position(Base, TimestampMixin):
    """Позиция в портфеле: купленный подарок и его судьба."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    gift_id: Mapped[int] = mapped_column(ForeignKey("gifts.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"), index=True)

    status: Mapped[PositionStatus] = mapped_column(
        EnumStr(PositionStatus), default=PositionStatus.HELD, index=True
    )

    #: Где физически лежит актив сейчас.
    custody_market: Mapped[Market] = mapped_column(EnumStr(Market), default=Market.TELEGRAM)

    # --- покупка ---
    buy_market: Mapped[Market] = mapped_column(EnumStr(Market))
    buy_price: Mapped[Decimal] = mapped_column(Money)
    buy_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    buy_currency: Mapped[Currency] = mapped_column(EnumStr(Currency), default=Currency.STARS)
    bought_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    buy_intent_id: Mapped[int | None] = mapped_column(ForeignKey("intents.id"))

    # --- выставление ---
    list_market: Mapped[Market | None] = mapped_column(EnumStr(Market))
    list_price: Mapped[Decimal | None] = mapped_column(Money)
    list_external_id: Mapped[str | None] = mapped_column(String(128))
    listed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_reprice_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    reprice_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Подарок нельзя перепродать/передать до этого момента (cooldown Telegram).
    resale_available_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    # --- продажа ---
    sold_price: Mapped[Decimal | None] = mapped_column(Money)
    sold_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    sold_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    note: Mapped[str | None] = mapped_column(Text)

    gift: Mapped[Gift] = relationship(back_populates="positions")

    @property
    def cost_basis(self) -> Decimal:
        """Полная себестоимость позиции."""
        return Decimal(self.buy_price) + Decimal(self.buy_fee)

    @property
    def realized_pnl(self) -> Decimal | None:
        """Реализованный PnL. None, пока позиция не продана."""
        if self.sold_price is None:
            return None
        return Decimal(self.sold_price) - Decimal(self.sold_fee) - self.cost_basis


# ----------------------------------------------------------------------
# Служебное
# ----------------------------------------------------------------------
class Candidate(Base, TimestampMixin):
    """Найденная возможность — то, что бот показывает владельцу."""

    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    gift_id: Mapped[int] = mapped_column(ForeignKey("gifts.id"), index=True)
    market: Mapped[Market] = mapped_column(EnumStr(Market), index=True)
    listing_external_id: Mapped[str] = mapped_column(String(128))

    #: Цена, приведённая к Stars — для сравнения площадок между собой.
    price_stars: Mapped[Decimal] = mapped_column(Money)
    #: Цена в валюте площадки. Именно её принимает адаптер при покупке:
    #: для Portals/MRKT это TON, и подставлять сюда Stars нельзя.
    price_native: Mapped[Decimal | None] = mapped_column(Money)
    native_currency: Mapped[Currency] = mapped_column(
        EnumStr(Currency), default=Currency.STARS
    )
    fair_value_stars: Mapped[Decimal] = mapped_column(Money)
    net_roi: Mapped[Decimal] = mapped_column(Money, index=True)
    risk_score: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[Confidence] = mapped_column(EnumStr(Confidence))
    #: Человекочитаемое обоснование решения.
    rationale: Mapped[dict] = mapped_column(JSON, default=dict)

    #: pending | approved | rejected | executed | expired
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("intents.id"))

    gift: Mapped[Gift] = relationship()
    strategy: Mapped[Strategy] = relationship()


class Setting(Base, TimestampMixin):
    """Runtime-настройки, меняемые из UI без перезапуска."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    #: Секреты хранятся зашифрованными (app.crypto).
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Аудит всех значимых действий — требование security-блока ТЗ."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
