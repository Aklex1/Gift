"""Смена ключа шифрования без потери секретов.

Ключ `GIFT_SECRET_KEY` шифрует токены площадок и api_hash аккаунтов.
Сменить его простой правкой .env нельзя: старые значения перестанут
расшифровываться, и площадки молча отвалятся — торговля продолжится,
но заявки начнут отбиваться по 401, а причина будет неочевидна.

Здесь смена делается правильно: всё расшифровывается старым ключом,
зашифровывается новым и записывается одной транзакцией. Если хоть
одно значение не читается старым ключом, операция отменяется целиком —
частично перешифрованная база хуже исходной.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re

from cryptography.fernet import Fernet, InvalidToken

from app.crypto import _PREFIX  # noqa: PLC2701 - единый формат хранения
from app.db import session_scope
from app.models import Account, AuditLog, Setting

log = logging.getLogger(__name__)

ENV_KEY = "GIFT_SECRET_KEY"


def _fernet_for(key: str) -> Fernet:
    """Построить Fernet для конкретного ключа.

    Повторяет логику `app.crypto._fernet`, но для произвольного
    значения, а не только для текущего из настроек: при ротации нужно
    держать в руках сразу два ключа.
    """
    raw = (key or "").strip()
    if not raw:
        raise ValueError("Ключ пуст")
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def _validate_key(key: str) -> bool:
    """Годится ли значение как ключ шифрования."""
    try:
        _fernet_for(key)
    except ValueError:
        return False
    return True


def generate_key() -> str:
    """Сгенерировать новый ключ (тот же формат, что у установщика)."""
    return Fernet.generate_key().decode()


def plan(new_key: str = "") -> dict:
    """Посчитать, что будет перешифровано, ничего не меняя."""
    with session_scope() as session:
        rows = session.query(Setting).filter_by(is_secret=True).all()
        accounts = session.query(Account).all()
        return {
            "settings": sorted(
                row.key for row in rows if (row.value or "").startswith(_PREFIX)
            ),
            "accounts": sorted(
                a.name for a in accounts if (a.api_hash_enc or "").startswith(_PREFIX)
            ),
            "new_key_valid": _validate_key(new_key) if new_key else None,
        }


def rotate(old_key: str, new_key: str, *, actor: str = "cli") -> dict:
    """Перешифровать все секреты новым ключом.

    Returns:
        Сколько настроек и аккаунтов перешифровано.

    Raises:
        ValueError: новый ключ негоден, совпадает со старым либо
            старый ключ не подходит к данным (тогда база не меняется).
    """
    if not _validate_key(new_key):
        raise ValueError("Новый ключ негоден")
    if (old_key or "").strip() == (new_key or "").strip():
        raise ValueError("Новый ключ совпадает со старым")

    old = _fernet_for(old_key)
    new = _fernet_for(new_key)

    def recrypt(value: str, where: str) -> str:
        """Перешифровать одно значение."""
        try:
            plain = old.decrypt(value[len(_PREFIX) :].encode())
        except InvalidToken as exc:
            raise ValueError(
                f"Старый ключ не подходит к значению ({where}). "
                "Ничего не изменено."
            ) from exc
        return _PREFIX + new.encrypt(plain).decode()

    report = {"settings": 0, "accounts": 0}

    # Одна транзакция: либо перешифровано всё, либо ничего. Исключение
    # внутри session_scope откатывает всё, что уже успели поменять.
    with session_scope() as session:
        for row in session.query(Setting).filter_by(is_secret=True).all():
            if not (row.value or "").startswith(_PREFIX):
                continue
            row.value = recrypt(row.value, f"настройка {row.key}")
            report["settings"] += 1

        for account in session.query(Account).all():
            if not (account.api_hash_enc or "").startswith(_PREFIX):
                continue
            account.api_hash_enc = recrypt(
                account.api_hash_enc, f"аккаунт {account.name}"
            )
            report["accounts"] += 1

        session.add(
            AuditLog(
                actor=actor,
                action="security.key_rotated",
                target=ENV_KEY,
                payload=dict(report),
            )
        )

    log.warning(
        "Ключ шифрования сменён: настроек %s, аккаунтов %s",
        report["settings"],
        report["accounts"],
    )
    return report


def verify(key: str) -> dict:
    """Проверить, что все секреты читаются данным ключом.

    Нужно и перед сменой ключа, и после восстановления из бэкапа:
    иначе непригодность копии выясняется уже в бою.
    """
    fernet = _fernet_for(key)
    ok, failed = 0, []

    def check(value: str, where: str) -> None:
        """Пробное расшифрование одного значения."""
        nonlocal ok
        try:
            fernet.decrypt(value[len(_PREFIX) :].encode())
        except InvalidToken:
            failed.append(where)
        else:
            ok += 1

    with session_scope() as session:
        for row in session.query(Setting).filter_by(is_secret=True).all():
            if (row.value or "").startswith(_PREFIX):
                check(row.value, f"настройка {row.key}")
        for account in session.query(Account).all():
            if (account.api_hash_enc or "").startswith(_PREFIX):
                check(account.api_hash_enc, f"аккаунт {account.name}")

    return {"ok": ok, "failed": failed}


def read_env_key(env_path) -> str:
    """Достать текущий ключ прямо из .env, не полагаясь на процесс."""
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{ENV_KEY}="):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    return ""


def write_env_key(env_path, new_key: str) -> bool:
    """Записать новый ключ в .env, сохранив остальной файл.

    Возвращает False, если строки с ключом в файле нет — тогда вызывающий
    должен вписать её сам и не делать вид, что всё готово.
    """
    if not env_path.exists():
        return False
    text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{ENV_KEY}=.*$", re.MULTILINE)
    if not pattern.search(text):
        return False
    env_path.write_text(pattern.sub(f"{ENV_KEY}={new_key}", text), encoding="utf-8")
    return True
