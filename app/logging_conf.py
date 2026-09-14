"""Настройка логирования с редактированием секретов."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

from app.config import settings

#: Шаблоны, значения которых никогда не должны попасть в лог.
_SECRET_PATTERNS = [
    re.compile(r"(api_hash[\"'=:\s]+)([A-Za-z0-9_\-]{8,})", re.I),
    re.compile(r"(bot_token[\"'=:\s]+)([0-9]{6,}:[A-Za-z0-9_\-]{20,})", re.I),
    re.compile(r"(\b\d{6,}:[A-Za-z0-9_\-]{30,})"),
    re.compile(r"(Authorization[\"'=:\s]+)(\S+)", re.I),
    re.compile(r"(auth[\"'=:\s]+)([A-Za-z0-9_\-\.]{16,})", re.I),
]


class RedactingFilter(logging.Filter):
    """Вырезает похожие на секреты подстроки из сообщений."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - логгер не должен падать
            return True
        redacted = msg
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(
                lambda m: (m.group(1) + "***") if m.lastindex and m.lastindex >= 2 else "***",
                redacted,
            )
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(name: str = "gift") -> logging.Logger:
    """Инициализировать корневой логгер: stdout + файл."""
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    stream.addFilter(RedactingFilter())
    root.addHandler(stream)

    try:
        log_dir: Path = settings.data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / f"{name}.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.addFilter(RedactingFilter())
        root.addHandler(file_handler)
    except OSError:
        # Нет прав на каталог данных — работаем только в stdout.
        pass

    for noisy in ("httpx", "httpcore", "telethon", "aiosqlite", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)
