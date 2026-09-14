"""Общее хранилище настроек в БД.

Процессы бота, воркера и панели работают раздельно. Настройка,
изменённая в одном из них, должна дойти до остальных — иначе,
например, аварийный стоп из панели не остановит торгующий воркер.

Поэтому значения живут в таблице ``settings``, а в памяти процесса
держатся не дольше нескольких секунд.
"""

from __future__ import annotations

import logging
import time

from app.crypto import decrypt, encrypt
from app.db import session_scope
from app.models import Setting

log = logging.getLogger(__name__)

#: Сколько секунд значение считается свежим внутри процесса.
#: Должно быть заметно меньше интервала сканирования, чтобы
#: выключатель срабатывал быстро.
CACHE_TTL = 5.0

#: key -> (значение, момент чтения)
_cache: dict[str, tuple[str | None, float]] = {}


def get(key: str) -> str | None:
    """Прочитать значение. None, если не задано."""
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[1] < CACHE_TTL:
        return cached[0]

    try:
        with session_scope() as session:
            row = session.get(Setting, key)
            raw = row.value if row is not None else None
            secret = bool(row.is_secret) if row is not None else False
    except Exception as exc:  # noqa: BLE001 - БД недоступна, работаем на .env
        log.debug("Настройка %s недоступна: %s", key, exc)
        return None

    if raw and secret:
        try:
            raw = decrypt(raw)
        except RuntimeError as exc:
            # Ключ шифрования сменился. Валить процесс нельзя: одна
            # нечитаемая настройка не должна останавливать бота.
            log.error(
                "Настройка %s не расшифрована (%s). Используется значение "
                "из .env.",
                key,
                exc,
            )
            raw = None

    value = raw or None
    _cache[key] = (value, now)
    return value


def set(key: str, value: str | None, *, secret: bool = False) -> None:  # noqa: A001
    """Записать значение. Пустое значение удаляет настройку."""
    value = (value or "").strip()
    with session_scope() as session:
        row = session.get(Setting, key)
        if not value:
            if row is not None:
                session.delete(row)
        else:
            stored = encrypt(value) if secret else value
            if row is None:
                session.add(Setting(key=key, value=stored, is_secret=secret))
            else:
                row.value = stored
                row.is_secret = secret
    _cache.pop(key, None)


def invalidate(key: str | None = None) -> None:
    """Сбросить кэш целиком или по одному ключу."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)
