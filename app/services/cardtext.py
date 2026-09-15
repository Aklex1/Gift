"""Карточка находки — то, что владелец видит вместо строки в таблице.

Таблица кандидатов отвечает на вопрос «что нашлось». Карточка отвечает
на другой: **почему это дёшево** и **где здесь деньги**. Разница не
косметическая: по строке «ROI 26%» решение принять нельзя, не открыв
панель и не проверив пять чисел вручную.

Правило у всего модуля одно: не называть того, чего не измеряли.
Каждая строка либо опирается на число из обоснования кандидата, либо
не печатается вовсе. Пустая строка честнее правдоподобной — это уже
стоило нам ROI в 36 000%, нарисованного одним устаревшим курсом.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from app.enums import Currency, Market, display_currency

#: Ссылка на сам подарок в Telegram. Работает для любой площадки:
#: адрес складывается из коллекции и номера, а они есть везде.
GIFT_LINK = "https://t.me/nft/{slug}"

#: Ссылки в мини-приложения площадок — там, где лот можно открыть
#: прямо на месте продажи.
VENUE_LINK = {
    Market.MRKT: "https://t.me/mrkt/app?startapp={external_id}",
}


def num(raw) -> Decimal | None:
    """Число из обоснования — или ничего.

    В rationale цифры лежат строками: так они переживают JSON без
    потери точности. Испорченное значение здесь становится пустым
    местом, а не нулём — ноль прочитался бы как измеренный.
    """
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None


def stars(value: Decimal | None) -> str:
    """Звёзды без дробной части: доли звезды ничего не решают."""
    return "—" if value is None else f"{value.quantize(Decimal('1'))}"


def gift_slug(collection: str | None, number: int | None) -> str | None:
    """Адрес подарка в Telegram: «LightSword-77735»."""
    if not collection or number is None:
        return None
    return f"{''.join(str(collection).split())}-{number}"


def links(*, collection, number, market: Market, external_id: str) -> list[str]:
    """Куда пойти смотреть лот.

    Сначала сам подарок, потом площадка: подарок открывается у всех, а
    мини-приложение — только у того, кто в него заходил.
    """
    out: list[str] = []
    slug = gift_slug(collection, number)
    if slug:
        out.append(GIFT_LINK.format(slug=slug))
    template = VENUE_LINK.get(market)
    if template and external_id:
        out.append(template.format(external_id=external_id))
    return out


def double_margin(attributes: list[dict]) -> bool:
    """Держат ли цену два признака сразу, а не один.

    Один редкий признак — это ставка на то, что покупатель ищет именно
    его. Два — два независимых повода купить, и если ошиблись в одном,
    остаётся второй.
    """
    return sum(1 for a in attributes if (num(a.get("premium")) or 0) > 0) >= 2


def attribute_line(attributes: list[dict]) -> str:
    """Запас по каждому признаку: «модель +12.6% · фон +26.5%»."""
    parts = []
    for item in attributes:
        premium = num(item.get("premium"))
        if premium is None:
            continue
        parts.append(f"{item.get('label', '?')} {premium:+.1%}")
    return " · ".join(parts)


def shelf_line(attributes: list[dict]) -> str:
    """Самый сильный признак — словами, с его floor'ом и тиражом.

    Ранга лота на полке здесь нет намеренно: чтобы сказать «место 3 из
    25», нужно перечислить все лоты с этим признаком, а это отдельный
    запрос на каждого кандидата. Печатать вместо ранга догадку значит
    выдавать за измерение то, чего не измеряли.
    """
    if not attributes:
        return ""
    top = attributes[0]
    floor = num(top.get("floor"))
    if floor is None:
        return ""
    line = f"{top.get('label', '?')} «{top.get('name')}» — floor {stars(floor)} ★"
    supply = top.get("supply")
    if supply:
        line += f", выпущено {supply}"
    return line


def lines(
    *,
    name: str,
    market: Market,
    external_id: str,
    collection: str | None,
    number: int | None,
    model: str | None,
    backdrop: str | None,
    price_native: Decimal | None,
    currency: Currency,
    price_usd: Decimal | None,
    profit_usd: Decimal | None,
    rationale: dict,
    days_to_sell: float | None = None,
) -> list[tuple[str, str]]:
    """Строки карточки: значок и текст, без разметки.

    Отдельно от отправки намеренно. Карточку показывают и бот, и
    панель, а две копии одной логики расходятся — это уже случилось
    сегодня с проверкой «сканер молчит», которая была написана дважды
    и в одном из мест осталась старой.

    Args:
        rationale: обоснование кандидата как его сохранил сканер.

    Returns:
        Пары «значок, текст». Пустой значок — строка-продолжение.
    """
    attributes = rationale.get("attributes") or []
    sales = rationale.get("sales") or {}
    out: list[tuple[str, str]] = [("🎁", f"{name} · {market.value}")]

    traits = " / ".join(x for x in (model, backdrop) if x)
    if traits:
        out.append(("", traits))

    if price_native is not None:
        price = f"{price_native} {display_currency(currency)}"
        if price_usd is not None:
            price += f" · {price_usd:.2f} $"
        out.append(("💰", price))

    out.append((
        "⭐",
        f"Прогноз: {stars(num(rationale.get('fair_value')))} ★"
        f" · продажа: {stars(num(rationale.get('expected_sale_price')))} ★"
        f" · ноль: {stars(num(rationale.get('break_even')))} ★",
    ))

    roi = num(rationale.get("net_roi"))
    profit = "Прибыль: "
    profit += "—" if profit_usd is None else f"{profit_usd:+.2f} $"
    if roi is not None:
        profit += f" · запас: {roi:+.1%}"
    risk = rationale.get("risk_score")
    if risk is not None:
        profit += f" · риск: {risk}"
    out.append(("📈", profit))

    line = attribute_line(attributes)
    if line:
        out.append(("🎯", line))
    shelf = shelf_line(attributes)
    if shelf:
        out.append(("🧭", f"Полка: {shelf}"))

    hint = rationale.get("better_sale") or {}
    if hint.get("market"):
        out.append((
            "💱",
            f"Выгоднее продать на {hint['market']}: {hint.get('net_roi', '')}",
        ))

    tail = []
    if sales.get("sales"):
        tail.append(f"продажи за 30 дн.: {sales['sales']}")
    if days_to_sell:
        tail.append(f"продажа ≈ {days_to_sell:.0f} дн.")
    if sales.get("source"):
        tail.append(f"источник {sales['source']}")
    if tail:
        out.append(("⏱", " · ".join(tail)))

    if double_margin(attributes):
        out.append(("💎", "Двойной запас: цену держат два признака, а не один"))

    for url in links(
        collection=collection, number=number,
        market=market, external_id=external_id,
    ):
        out.append(("🔗", url))
    return out


def render(**kwargs) -> str:
    """Карточка для Telegram — те же строки, с разметкой HTML."""
    rows = lines(**kwargs)
    body = []
    for i, (icon, text) in enumerate(rows):
        prefix = f"{icon} " if icon else ""
        body.append(f"{prefix}<b>{text}</b>" if i == 0 else f"{prefix}{text}")
    return "🔎 <b>Находка</b>\n" + "\n".join(body)
