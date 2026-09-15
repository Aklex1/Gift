"""Канонические перечисления домена.

Все состояния, режимы и флаги возможностей живут здесь, чтобы адаптеры,
сервисы и UI говорили на одном языке.
"""

from __future__ import annotations

import enum


class ValueStr(str, enum.Enum):
    """Строковое перечисление, которое печатается своим значением.

    В Python 3.11 ``str(SomeEnum.X)`` у смешанного str-enum возвращает
    "SomeEnum.X", а не "x". Это ломало запись в БД, шаблоны и логи,
    поэтому ``__str__`` переопределён явно.
    """

    def __str__(self) -> str:
        return str(self.value)


class Market(ValueStr):
    """Торговые площадки, поддерживаемые системой."""

    TELEGRAM = "telegram"          # официальный MTProto resale-маркет
    PORTALS = "portals"            # portals.tg, приватный TMA API
    MRKT = "mrkt"                  # mrkt.land, приватный TMA API
    TONNEL = "tonnel"              # marketplace.tonnel.network, приватный API
    GETGEMS = "getgems"            # getgems.io, публичный API + on-chain
    FRAGMENT = "fragment"          # fragment.com, витрина Telegram, только чтение


class Capability(ValueStr):
    """Операции, которые адаптер площадки может уметь."""

    SEARCH = "search"              # поиск активных лотов
    HISTORY = "history"            # история продаж
    BALANCE = "balance"            # баланс аккаунта на площадке
    INVENTORY = "inventory"        # собственные подарки
    BUY = "buy"                    # покупка лота
    LIST = "list"                  # выставление на продажу
    REPRICE = "reprice"            # изменение цены лота
    CANCEL = "cancel"              # снятие с продажи
    RECONCILE = "reconcile"        # сверка исхода операции
    TRANSFER = "transfer"          # передача подарка


class CapabilityStatus(ValueStr):
    """Уровень доверия к конкретной возможности адаптера.

    Прямо следует из аудита ТЗ: без партнёрского API приватные площадки
    не могут иметь статус выше EXPERIMENTAL.
    """

    SUPPORTED = "supported"        # официальный API, можно пускать в AUTO
    EXPERIMENTAL = "experimental"  # приватный/reverse-engineered, без SLA
    UNAVAILABLE = "unavailable"    # не реализовано или отключено конфигом


class TradeMode(ValueStr):
    """Модель разрешений на исполнение.

    SAFE — только рекомендации, ни одного write-вызова.
    SEMI — write-вызов только после явного подтверждения владельцем.
    AUTO — автономное исполнение в пределах whitelisted capabilities,
           лимитов бюджета и при выключенном kill switch.
    """

    SAFE = "safe"
    SEMI = "semi"
    AUTO = "auto"


class IntentStatus(ValueStr):
    """Состояния execution saga.

    Переходы строго однонаправленные. UNKNOWN — терминальное до сверки
    состояние: слепой retry из него запрещён.
    """

    PLANNED = "planned"            # намерение создано, деньги не тронуты
    RESERVED = "reserved"          # бюджет атомарно зарезервирован
    SUBMITTED = "submitted"        # внешний вызов отправлен
    CONFIRMED = "confirmed"        # исход подтверждён площадкой
    FAILED = "failed"              # явный отказ, резерв освобождён
    UNKNOWN = "unknown"            # исход неизвестен, требуется сверка
    RECONCILED = "reconciled"      # исход сверен с внешним состоянием
    CANCELLED = "cancelled"        # отменено до отправки
    EXPIRED = "expired"            # резерв истёк без отправки


TERMINAL_INTENT_STATUSES = frozenset(
    {
        IntentStatus.CONFIRMED,
        IntentStatus.FAILED,
        IntentStatus.RECONCILED,
        IntentStatus.CANCELLED,
        IntentStatus.EXPIRED,
    }
)

#: Разрешённые переходы execution saga.
INTENT_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.PLANNED: frozenset({IntentStatus.RESERVED, IntentStatus.CANCELLED}),
    IntentStatus.RESERVED: frozenset(
        {IntentStatus.SUBMITTED, IntentStatus.CANCELLED, IntentStatus.EXPIRED}
    ),
    IntentStatus.SUBMITTED: frozenset(
        {IntentStatus.CONFIRMED, IntentStatus.FAILED, IntentStatus.UNKNOWN}
    ),
    IntentStatus.UNKNOWN: frozenset({IntentStatus.RECONCILED}),
    IntentStatus.CONFIRMED: frozenset({IntentStatus.RECONCILED}),
    IntentStatus.FAILED: frozenset({IntentStatus.RECONCILED}),
    IntentStatus.RECONCILED: frozenset(),
    IntentStatus.CANCELLED: frozenset(),
    IntentStatus.EXPIRED: frozenset(),
}


class IntentKind(ValueStr):
    """Тип торгового намерения."""

    BUY = "buy"
    LIST = "list"
    REPRICE = "reprice"
    CANCEL = "cancel"
    TRANSFER = "transfer"


class Currency(ValueStr):
    """Расчётные валюты. Курсы фиксируются снапшотом с таймстемпом.

    Значения — то, что лежит в базе и уходит во внешние API, и менять
    их нельзя: TonAPI и площадки говорят «TON», и каждая уже
    сохранённая строка тоже. Человеку же показывается ``display``.
    """

    STARS = "STARS"                # Telegram Stars
    TON = "TON"                    # в интерфейсе — GRAM, см. DISPLAY_NAMES
    USD = "USD"
    RUB = "RUB"

    @property
    def display(self) -> str:
        """Название валюты для человека."""
        return DISPLAY_NAMES.get(self, self.value)


#: Как валюта называется в интерфейсе.
#:
#: 15 июня 2026 сеть переименовала токен: TON стал GRAM, курс 1:1,
#: балансы и цены не изменились. Площадки, кошельки и посты говорят
#: «GRAM», поэтому и бот показывает GRAM — но внутри и в запросах к
#: API остаётся TON, иначе пришлось бы переписывать каждую строку в
#: базе и ломать совместимость с внешними сервисами.
DISPLAY_NAMES: dict[Currency, str] = {
    Currency.TON: "GRAM",
}


def display_currency(value: "Currency | str | None") -> str:
    """Название валюты для человека, из чего угодно.

    Принимает и Currency, и строку из базы, и None — в шаблонах и
    логах встречается всё перечисленное.
    """
    if value is None:
        return "—"
    if isinstance(value, Currency):
        return value.display
    try:
        return Currency(str(value).upper()).display
    except ValueError:
        return str(value)


class PositionStatus(ValueStr):
    """Жизненный цикл позиции в портфеле."""

    HELD = "held"                  # в инвентаре, не выставлено
    LOCKED = "locked"              # cooldown/transfer lock после покупки
    LISTED = "listed"              # выставлено на продажу
    SOLD = "sold"                  # продано, выручка получена
    TRANSFERRED = "transferred"    # выведено из-под управления бота


class ReservationStatus(ValueStr):
    """Состояние резерва в бюджетном ledger."""

    ACTIVE = "active"
    RELEASED = "released"          # освобождён (отказ/отмена)
    SETTLED = "settled"            # списан по факту сделки
    EXPIRED = "expired"            # протух по TTL


class Confidence(ValueStr):
    """Качество выборки рыночных данных для оценки."""

    HIGH = "high"                  # достаточно свежих независимых сделок
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"                  # данных нет, торговать нельзя
