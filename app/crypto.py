"""Шифрование секретов, хранимых в БД.

Токены площадок и строка Telethon-сессии никогда не лежат в базе
открытым текстом: используется Fernet (AES-128-CBC + HMAC) на ключе
из GIFT_SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_PREFIX = "enc::"


def generate_key() -> str:
    """Сгенерировать новый ключ шифрования (для установщика)."""
    return Fernet.generate_key().decode()


def _fernet() -> Fernet:
    """Построить Fernet из GIFT_SECRET_KEY.

    Если ключ задан не в формате Fernet, он детерминированно
    разворачивается в 32 байта через SHA-256 — чтобы установщику
    было достаточно любой длинной случайной строки.
    """
    raw = (settings.secret_key or "").strip()
    if not raw:
        raise RuntimeError(
            "GIFT_SECRET_KEY не задан. Сгенерируйте: python -m app.cli gen-key"
        )
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str | None) -> str | None:
    """Зашифровать значение. None и пустая строка проходят насквозь."""
    if not value:
        return value
    if value.startswith(_PREFIX):
        return value
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    """Расшифровать значение, записанное `encrypt`."""
    if not value:
        return value
    if not value.startswith(_PREFIX):
        # Значение записано до включения шифрования — отдаём как есть.
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX) :].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError(
            "Не удалось расшифровать секрет: GIFT_SECRET_KEY изменился "
            "или данные повреждены."
        ) from exc


def redact(value: str | None, keep: int = 4) -> str:
    """Замаскировать секрет для логов и UI."""
    if not value:
        return "—"
    plain = value[len(_PREFIX) :] if value.startswith(_PREFIX) else value
    if len(plain) <= keep:
        return "*" * len(plain)
    return f"{'*' * 8}{plain[-keep:]}"
