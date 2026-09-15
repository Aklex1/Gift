"""Хранилище ключей и токенов в БД.

Зачем отдельно от .env: токены площадок живут часы и меняются часто.
Править файл на сервере и перезапускать сервисы ради каждого токена —
неудобно, поэтому значения хранятся в таблице ``settings``
в зашифрованном виде и редактируются из веб-панели.

Приоритет: значение из БД важнее значения из .env. Пустое значение
в БД означает «не задано», и тогда берётся .env.

Значения кэшируются на короткое время: процессы бота, воркера и
панели работают раздельно, и изменение, сделанное в панели, должно
дойти до остальных без перезапуска.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from app.crypto import decrypt, encrypt, redact
from app.db import session_scope
from app.models import AuditLog, Setting

log = logging.getLogger(__name__)

#: Сколько секунд держать значение в памяти процесса.
CACHE_TTL = 20.0


@dataclass(frozen=True, slots=True)
class SecretField:
    """Описание редактируемого ключа для UI."""

    key: str
    title: str
    hint: str
    #: Секрет маскируется в интерфейсе и шифруется в БД.
    secret: bool = True
    #: Группа для вёрстки страницы настроек.
    group: str = "Прочее"
    placeholder: str = ""
    #: Имя проверки формата: опечатка не должна сохраняться молча.
    check: str = ""


#: Адрес кошелька TON: дружественная форма (48 символов base64url,
#: начинается с UQ/EQ/kQ/0Q) либо сырая форма "0:<64 hex>".
_TON_FRIENDLY = re.compile(r"^[UEk0]Q[A-Za-z0-9_-]{46}$")
_TON_RAW = re.compile(r"^-?\d:[0-9a-fA-F]{64}$")


def validate(key: str, value: str) -> str:
    """Проверить формат значения.

    Returns:
        Текст ошибки, либо пустая строка если всё в порядке.
    """
    field = FIELD_BY_KEY.get(key)
    if field is None or not value:
        return ""

    if field.check == "ton_address":
        cleaned = value.strip()
        if _TON_FRIENDLY.match(cleaned) or _TON_RAW.match(cleaned):
            return ""
        return (
            "не похоже на адрес GRAM (TON). Ожидается 48 символов, начинается "
            "с UQ или EQ (например UQAbc…), либо сырая форма 0:<64 hex>"
        )

    if field.check == "bot_token":
        if re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", value.strip()):
            return ""
        return "токен @BotFather выглядит как 7123456789:AAF… — проверьте, что скопирован целиком"

    if field.check == "int":
        if value.strip().isdigit():
            return ""
        return "ожидается число"

    if field.check == "id_list":
        for chunk in value.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk and not chunk.lstrip("-").isdigit():
                return f"«{chunk}» не является числовым Telegram id"
        return ""

    return ""


#: Всё, что можно задать из панели.
FIELDS: tuple[SecretField, ...] = (
    # --- Telegram: бот управления ---
    SecretField(
        key="BOT_TOKEN",
        title="Токен бота",
        hint="У @BotFather → /newbot. После смены перезапустите сервис бота.",
        group="Telegram — бот управления",
        placeholder="7123456789:AAF...",
        check="bot_token",
    ),
    SecretField(
        key="OWNER_IDS",
        title="Telegram ID владельцев",
        hint="Через запятую. Свой id узнаете командой /id в боте. "
             "Пусто = бот не отвечает никому.",
        secret=False,
        group="Telegram — бот управления",
        placeholder="123456789",
        check="id_list",
    ),
    # --- Telegram: торговый аккаунт ---
    SecretField(
        key="TG_API_ID",
        title="api_id",
        hint="my.telegram.org → API development tools. Число.",
        secret=False,
        group="Telegram — торговый аккаунт",
        placeholder="21724531",
        check="int",
    ),
    SecretField(
        key="TG_API_HASH",
        title="api_hash",
        hint="Оттуда же, 32 символа. После заполнения выполните на "
             "сервере: gift-cli login",
        group="Telegram — торговый аккаунт",
        placeholder="a1b2c3d4e5f6...",
    ),
    SecretField(
        key="TG_PHONE",
        title="Телефон аккаунта",
        hint="Нужен только для входа через gift-cli login.",
        secret=False,
        group="Telegram — торговый аккаунт",
        placeholder="+79991234567",
    ),
    # --- Площадки ---
    SecretField(
        key="PORTALS_AUTH",
        title="Portals: Authorization",
        hint="Можно не заполнять: бот сам открывает мини-приложение от "
             "имени торгового аккаунта и обновляет эту строку каждые "
             "6 часов. Вписывать вручную нужно, только если "
             "автопродление не работает (Telegram Web → Portals → F12 → "
             "Network → заголовок Authorization целиком).",
        group="Площадки",
        placeholder="tma query_id=AAH...",
    ),
    SecretField(
        key="PORTALS_MINIAPP",
        title="Portals: адрес мини-приложения",
        hint="Для автопродления токена, в формате бот:короткое_имя. "
             "По умолчанию portals:market — меняйте, только если "
             "площадка переехала и продление перестало работать.",
        secret=False,
        group="Площадки",
        placeholder="portals:market",
    ),
    SecretField(
        key="MRKT_INIT_DATA",
        title="MRKT: initData",
        hint="Тоже обновляется автоматически. Бот меняет initData на "
             "токен сам; вручную заполняют только при сбое "
             "автопродления (поле data из запроса /auth мини-приложения).",
        group="Площадки",
        placeholder="query_id=AAH...&user=%7B%22id%22...",
    ),
    SecretField(
        key="MRKT_MINIAPP",
        title="MRKT: адрес мини-приложения",
        hint="Для автопродления токена, в формате бот:короткое_имя. "
             "По умолчанию mrkt:app.",
        secret=False,
        group="Площадки",
        placeholder="mrkt:app",
    ),
    SecretField(
        key="MRKT_AUTH",
        title="MRKT: готовый токен",
        hint="Альтернатива initData, если обмен не работает.",
        group="Площадки",
        placeholder="eyJhbGci...",
    ),
    SecretField(
        key="TONNEL_AUTH",
        title="Tonnel: Authorization",
        hint="Аналогично Portals. Часто блокируется Cloudflare.",
        group="Площадки",
        placeholder="Bearer eyJ...",
    ),
    SecretField(
        key="GETGEMS_API_KEY",
        title="Getgems: ключ API",
        hint="api.getgems.io/public-api/docs",
        group="Площадки",
    ),
    # --- Канал находок ---
    SecretField(
        key="FEED_CHANNEL",
        title="Канал находок",
        hint="Канал, где чужой бот публикует удачные покупки "
             "(#находка_дня). Подойдёт @имя, ссылка t.me или ссылка из "
             "веб-клиента. Торговый аккаунт должен быть подписан на "
             "канал — читать бот будет от его имени.",
        secret=False,
        group="Канал находок",
        placeholder="https://t.me/имя_канала",
    ),
    SecretField(
        key="FEED_DEPTH",
        title="Сколько сообщений читать",
        hint="За один проход. По умолчанию 50 — этого хватает на пару "
             "недель ежедневных постов.",
        secret=False,
        group="Канал находок",
        placeholder="50",
    ),

    # --- TON ---
    SecretField(
        key="TONAPI_KEY",
        title="TonAPI: ключ",
        hint="tonconsole.com → раздел «TON API» (сервис сохранил прежнее "
             "название). Нужен только для чтения баланса GRAM.",
        group="GRAM (только чтение)",
    ),
    SecretField(
        key="TON_WALLET_ADDRESS",
        title="Адрес кошелька GRAM",
        hint="Только для наблюдения за балансом: бот не тратит GRAM и не "
             "хранит приватный ключ. Скопируйте адрес из @wallet или "
             "Tonkeeper. Для торговли подарками в Telegram нужны Stars, "
             "а не GRAM — это поле можно оставить пустым.",
        secret=False,
        group="GRAM (только чтение)",
        placeholder="UQAbc...",
        check="ton_address",
    ),
)

FIELD_BY_KEY = {field.key: field for field in FIELDS}

#: key -> (значение, момент чтения)
_cache: dict[str, tuple[str | None, float]] = {}


def _is_secret(key: str) -> bool:
    """Нужно ли шифровать это значение."""
    field = FIELD_BY_KEY.get(key)
    return field.secret if field else True


def get(key: str) -> str | None:
    """Прочитать значение из БД с учётом кэша.

    Возвращает None, если ключ не задан.
    """
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[1] < CACHE_TTL:
        return cached[0]

    try:
        with session_scope() as session:
            row = session.get(Setting, key)
            raw = row.value if row is not None else None
    except Exception as exc:  # noqa: BLE001 - БД недоступна, работаем на .env
        log.debug("Не удалось прочитать настройку %s: %s", key, exc)
        return None

    try:
        value = decrypt(raw) if raw else None
    except RuntimeError as exc:
        # Ключ шифрования сменился или потерян. Валить процесс нельзя:
        # иначе один нечитаемый секрет останавливает весь бот.
        log.error(
            "Секрет %s не расшифрован (%s). Значение игнорируется, "
            "используется .env. Задайте прежний GIFT_SECRET_KEY либо "
            "введите ключ заново в панели.",
            key,
            exc,
        )
        value = None

    value = value or None
    _cache[key] = (value, now)
    return value


def resolve(key: str, fallback: str = "") -> str:
    """Значение из БД, иначе из .env.

    Единственный способ получить токен в коде адаптеров.
    """
    return get(key) or fallback or ""


def set_value(key: str, value: str | None, *, actor: str = "web") -> None:
    """Сохранить значение. Пустая строка удаляет ключ."""
    value = (value or "").strip()
    with session_scope() as session:
        row = session.get(Setting, key)
        if not value:
            if row is not None:
                session.delete(row)
            action = "secret.clear"
        else:
            stored = encrypt(value) if _is_secret(key) else value
            if row is None:
                row = Setting(key=key, value=stored, is_secret=_is_secret(key))
                session.add(row)
            else:
                row.value = stored
                row.is_secret = _is_secret(key)
            action = "secret.set"

        session.add(
            AuditLog(
                actor=actor,
                action=action,
                target=key,
                # В аудит попадает только маска, не само значение.
                payload={"value": redact(value) if value else "—"},
            )
        )
    _cache.pop(key, None)
    log.info("Настройка %s обновлена (%s)", key, redact(value) if value else "очищено")


def masked_state() -> dict[str, dict]:
    """Состояние всех полей для интерфейса.

    Секреты не отдаются наружу: только признак «задано» и маска.
    """
    from app.config import settings as cfg

    env_fallback = {
        "BOT_TOKEN": cfg.bot_token,
        "OWNER_IDS": cfg.owner_ids,
        "TG_API_ID": str(cfg.tg_api_id or ""),
        "TG_API_HASH": cfg.tg_api_hash,
        "TG_PHONE": cfg.tg_phone,
        "PORTALS_AUTH": cfg.portals_auth,
        "MRKT_AUTH": cfg.mrkt_auth,
        "MRKT_INIT_DATA": cfg.mrkt_init_data,
        "TONNEL_AUTH": cfg.tonnel_auth,
        "GETGEMS_API_KEY": cfg.getgems_api_key,
        "TONAPI_KEY": cfg.tonapi_key,
        "TON_WALLET_ADDRESS": cfg.ton_wallet_address,
    }

    out: dict[str, dict] = {}
    for field in FIELDS:
        from_db = get(field.key)  # нечитаемый секрет вернётся как None
        from_env = env_fallback.get(field.key) or ""
        value = from_db or from_env
        out[field.key] = {
            "field": field,
            "is_set": bool(value),
            "source": "панель" if from_db else ("файл .env" if from_env else "—"),
            # Несекретные значения показываем целиком: их удобно править.
            "display": (redact(value) if field.secret else value) if value else "",
        }
    return out


def invalidate() -> None:
    """Сбросить кэш — например, после массового сохранения."""
    _cache.clear()
