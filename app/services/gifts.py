"""Работа с канонической идентичностью подарков."""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

from app.adapters.base import GiftRef, ListingDTO
from app.enums import Market
from app.models import Gift, Listing, utcnow
from app.services.marketdata import to_stars

log = logging.getLogger(__name__)


def upsert_gift(session: Session, ref: GiftRef) -> Gift:
    """Найти или создать канонический подарок по ссылке адаптера.

    Схлопывание одной и той же вещи с разных площадок в одну запись —
    обязательное условие кросс-рыночного сравнения цен.
    """
    key = ref.canonical_key
    gift = session.query(Gift).filter_by(canonical_key=key).one_or_none()
    if gift is None:
        gift = Gift(
            canonical_key=key,
            collection=ref.collection,
            number=ref.number,
            slug=ref.slug,
            model=ref.model,
            backdrop=ref.backdrop,
            symbol=ref.symbol,
            tg_gift_id=ref.tg_gift_id,
            nft_address=ref.nft_address,
            attributes=ref.attributes or {},
        )
        session.add(gift)
        session.flush()
        return gift

    # Дополняем недостающие атрибуты, не затирая уже известные.
    for attr in ("model", "backdrop", "symbol", "slug", "tg_gift_id", "nft_address", "number"):
        if getattr(gift, attr, None) is None and getattr(ref, attr, None) is not None:
            setattr(gift, attr, getattr(ref, attr))

    rarities = ref.attributes or {}
    for field_name in ("model_rarity", "backdrop_rarity", "symbol_rarity"):
        value = rarities.get(field_name)
        if value is not None and getattr(gift, field_name, None) is None:
            setattr(gift, field_name, value)
    return gift


def upsert_listing(session: Session, dto: ListingDTO) -> Listing:
    """Записать или обновить активный лот."""
    gift = upsert_gift(session, dto.gift)
    listing = (
        session.query(Listing)
        .filter_by(market=dto.market, external_id=dto.external_id)
        .one_or_none()
    )
    price_stars = to_stars(session, dto.price, dto.currency)

    if listing is None:
        listing = Listing(
            gift_id=gift.id,
            market=dto.market,
            external_id=dto.external_id,
            price=dto.price,
            currency=dto.currency,
            price_stars=price_stars,
            seller=dto.seller,
            is_active=True,
            seen_at=utcnow(),
            raw=dto.raw or {},
        )
        session.add(listing)
    else:
        listing.gift_id = gift.id
        listing.price = dto.price
        listing.currency = dto.currency
        listing.price_stars = price_stars
        listing.seller = dto.seller
        listing.is_active = True
        listing.seen_at = utcnow()
        listing.raw = dto.raw or {}
    return listing


def deactivate_missing(
    session: Session, market: Market, seen_ids: set[str]
) -> int:
    """Пометить неактивными лоты, пропавшие из последнего скана."""
    query = session.query(Listing).filter(
        Listing.market == market, Listing.is_active.is_(True)
    )
    count = 0
    for listing in query.all():
        if listing.external_id not in seen_ids:
            listing.is_active = False
            count += 1
    return count


def describe(gift: Gift) -> str:
    """Человекочитаемое имя подарка для сообщений бота."""
    parts = [gift.collection]
    if gift.number is not None:
        parts.append(f"#{gift.number}")
    traits = [t for t in (gift.model, gift.backdrop, gift.symbol) if t]
    if traits:
        parts.append("(" + ", ".join(traits) + ")")
    return " ".join(parts)


def format_stars(value: Decimal | None) -> str:
    """Отформатировать сумму в Stars."""
    if value is None:
        return "—"
    return f"{Decimal(value):,.0f}".replace(",", " ")
