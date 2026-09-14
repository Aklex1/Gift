"""Автоматическое получение initData мини-приложений площадок.

Токены Portals и MRKT — это не постоянные API-ключи, а `initData`
Telegram Mini App: строка, которую Telegram выдаёт приложению при
открытии и которая живёт считанные часы. Поэтому «вписал токен один
раз» не работает: через сутки торговля встаёт на 401, и человеку
приходится лезть в DevTools за новой строкой.

Здесь та же строка берётся программно — тем же способом, каким её
получает настоящий клиент Telegram: MTProto-запросом от имени уже
авторизованного торгового аккаунта. Никаких паролей и кодов не нужно,
сессия уже есть.

Что важно понимать: initData подписан ключом бота площадки и
действителен только для неё. Мы ничего не подделываем — мы открываем
мини-приложение так же, как это сделал бы владелец аккаунта пальцем.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

from app.enums import Market

log = logging.getLogger(__name__)

#: Мини-приложения площадок: бот и короткое имя приложения.
#: Имена взяты из публичных ссылок площадок (t.me/portals/market) и
#: проверить их без авторизованной сессии нельзя, поэтому короткое имя
#: — это подсказка, а не обязательное условие: если оно не подойдёт,
#: приложение открывается через кнопку меню бота, где имя не нужно.
#: Переопределяются настройками PORTALS_MINIAPP / MRKT_MINIAPP
#: в формате "бот:короткое_имя".
MINIAPPS: dict[Market, tuple[str, str]] = {
    Market.PORTALS: ("portals", "market"),
    Market.MRKT: ("mrkt", "app"),
}

#: Куда сохраняется полученная строка.
INIT_DATA_KEY = {
    Market.PORTALS: "PORTALS_AUTH",
    Market.MRKT: "MRKT_INIT_DATA",
}

#: Настройка с адресом мини-приложения.
MINIAPP_KEY = {
    Market.PORTALS: "PORTALS_MINIAPP",
    Market.MRKT: "MRKT_MINIAPP",
}


def _miniapp(market: Market) -> tuple[str, str]:
    """Бот и короткое имя мини-приложения площадки."""
    from app.services import secrets

    default = MINIAPPS.get(market)
    if default is None:
        raise ValueError(f"Для {market.value} мини-приложение не задано")

    raw = (secrets.resolve(MINIAPP_KEY[market], "") or "").strip()
    if not raw:
        return default
    bot, _, short = raw.partition(":")
    return bot.strip().lstrip("@") or default[0], short.strip() or default[1]


def extract_init_data(url: str) -> str:
    """Достать tgWebAppData из URL, который вернул Telegram.

    Telegram отдаёт адрес вида
    ``https://portals.tg/#tgWebAppData=query_id%3D...&tgWebAppVersion=7.0``
    — нужная строка лежит во фрагменте и закодирована дважды.
    """
    if not url:
        return ""
    fragment = urlparse(url).fragment or ""
    if not fragment:
        # Некоторые приложения получают параметры в query, а не во фрагменте.
        fragment = urlparse(url).query or ""

    values = parse_qs(fragment).get("tgWebAppData")
    if values and values[0]:
        return unquote(values[0])

    # Запасной разбор: параметр мог прийти без экранирования.
    match = re.search(r"tgWebAppData=([^&]+)", url)
    return unquote(match.group(1)) if match else ""


async def fetch_init_data(market: Market, *, account_id: int | None = None) -> str:
    """Открыть мини-приложение площадки и вернуть свежий initData.

    Raises:
        AuthRequired: торговый аккаунт Telegram не авторизован.
        ValueError: площадка не поддерживает такой способ или
            Telegram не вернул данные.
    """
    from telethon.tl.functions.messages import (
        RequestAppWebViewRequest,
        RequestWebViewRequest,
    )
    from telethon.tl.types import InputBotAppShortName

    from app.adapters import telegram_gateway

    bot, short_name = _miniapp(market)

    if account_id is None:
        tg = telegram_gateway.default_gateway()
    else:
        from app.db import session_scope
        from app.models import Account

        with session_scope() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise ValueError(f"Аккаунт {account_id} не найден")
            tg = telegram_gateway.gateway_for(account)

    client = await tg.client()
    entity = await client.get_entity(bot)

    # Два способа открыть приложение. Первый точнее, но требует знать
    # короткое имя; второй открывает приложение из кнопки меню бота и
    # короткого имени не требует — он и страхует нас от переименования.
    attempts = [
        (
            f"@{bot}/{short_name}",
            RequestAppWebViewRequest(
                peer=entity,
                app=InputBotAppShortName(bot_id=entity, short_name=short_name),
                platform="android",
                write_allowed=True,
            ),
        ),
        (
            f"кнопка меню @{bot}",
            RequestWebViewRequest(
                peer=entity,
                bot=entity,
                platform="android",
                from_bot_menu=True,
            ),
        ),
    ]

    errors: list[str] = []
    for label, request in attempts:
        try:
            result = await tg.call(request)
        except Exception as exc:  # noqa: BLE001 - пробуем следующий способ
            errors.append(f"{label}: {type(exc).__name__} {exc}")
            continue

        init_data = extract_init_data(getattr(result, "url", "") or "")
        if init_data:
            log.info(
                "%s: получен свежий initData через %s (%s симв.)",
                market.value,
                label,
                len(init_data),
            )
            return init_data
        errors.append(f"{label}: Telegram не вернул tgWebAppData")

    raise ValueError(
        f"{market.value}: не удалось открыть мини-приложение. "
        + "; ".join(errors)
        + f". Если площадка переименовала приложение, задайте "
        f"{MINIAPP_KEY[market]} в формате бот:короткое_имя."
    )


async def renew(market: Market, *, account_id: int | None = None) -> dict:
    """Обновить токен площадки и сохранить его.

    Returns:
        Что произошло: ``{"ok": bool, "market": str, "detail": str}``.
    """
    from app.services import secrets

    try:
        init_data = await fetch_init_data(market, account_id=account_id)
    except Exception as exc:  # noqa: BLE001 - причина уходит в отчёт
        log.warning("%s: обновить токен не удалось: %s", market.value, exc)
        return {"ok": False, "market": market.value, "detail": str(exc)}

    key = INIT_DATA_KEY[market]
    # Portals принимает initData прямо в заголовке, но с префиксом tma.
    value = f"tma {init_data}" if market is Market.PORTALS else init_data
    secrets.set_value(key, value, actor="auto")

    # MRKT меняет initData на собственный токен: старый нужно сбросить,
    # иначе адаптер продолжит ходить с протухшим.
    if market is Market.MRKT:
        secrets.set_value("MRKT_AUTH", "", actor="auto")

    return {"ok": True, "market": market.value, "detail": f"{key} обновлён"}


async def renew_all(markets=None, *, force: bool = False) -> list[dict]:
    """Обновить токены всех площадок, где это возможно."""
    targets = list(markets or MINIAPPS.keys())
    return [await ensure_fresh(market, force=force) for market in targets]


#: Насколько старым может быть initData, прежде чем его обновят.
#: Площадки обычно принимают строку сутки; обновляем заранее, чтобы
#: не ловить 401 в момент покупки.
MAX_AGE_SEC = 6 * 3600


def auth_date(value: str) -> int:
    """Момент выдачи initData (unix-время) или 0, если его там нет."""
    if not value:
        return 0
    payload = value[4:] if value.startswith("tma ") else value
    match = re.search(r"auth_date=(\d+)", payload)
    return int(match.group(1)) if match else 0


def age_seconds(market: Market) -> int | None:
    """Возраст сохранённого токена в секундах.

    ``None`` — токена нет или в нём нет метки времени, и тогда о
    свежести судить нельзя: такой токен обновляем по расписанию.
    """
    import time

    from app.services import secrets

    stored = secrets.resolve(INIT_DATA_KEY[market], "")
    issued = auth_date(stored)
    if not issued:
        return None
    return max(0, int(time.time()) - issued)


async def ensure_fresh(
    market: Market, *, max_age: int = MAX_AGE_SEC, force: bool = False
) -> dict:
    """Обновить токен, если он старше допустимого.

    Returns:
        Отчёт с полем ``skipped``, если обновление не потребовалось.
    """
    if not force:
        age = age_seconds(market)
        if age is not None and age < max_age:
            return {
                "ok": True,
                "market": market.value,
                "skipped": True,
                "detail": f"токен свежий ({age // 60} мин.)",
            }
    return await renew(market)
