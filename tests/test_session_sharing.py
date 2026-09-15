"""Тесты совместного доступа к файлу MTProto-сессии.

Файл сессии Telethon — это SQLite, и запись в него идёт при каждом
обновлении кэша сущностей. Два процесса на одном файле дают
«database is locked»: ровно это и случилось, когда панель впервые
полезла в Telegram за постами канала, пока файл держал воркер.

Владелец файла — воркер. Панель, бот и CLI работают с копией ключа
в памяти.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.adapters import telegram_gateway as tg


@pytest.fixture()
def session_file(tmp_path, monkeypatch):
    """Файл сессии в том виде, в каком его оставляет gift-cli login."""
    from telethon.crypto import AuthKey
    from telethon.sessions import SQLiteSession

    path = tmp_path / "trading.session"
    source = SQLiteSession(str(path.with_suffix("")))
    source.set_dc(2, "149.154.167.51", 443)
    source.auth_key = AuthKey(bytes(range(256)))
    source.save()
    source.close()

    monkeypatch.setattr(tg, "_prefer_detached", False)
    monkeypatch.setattr(tg, "_gateways", {})
    yield path
    tg.prefer_detached(False)


def _gateway(path, detached):
    """Шлюз с нужным режимом сессии."""
    return tg.TelegramGateway(
        api_id=1, api_hash="hash", session_path=path, detached=detached
    )


# --- сама подмена сессии ----------------------------------------------


def test_normal_mode_uses_file(session_file):
    """Воркер работает с файлом: сессия должна переживать перезапуск."""
    arg = _gateway(session_file, False)._session_arg()

    assert isinstance(arg, str)
    assert arg.endswith("trading")


def test_detached_mode_uses_memory(session_file):
    """Неосновной процесс получает сессию в памяти, а не путь к файлу."""
    from telethon.sessions import MemorySession

    assert isinstance(_gateway(session_file, True)._session_arg(), MemorySession)


def test_detached_session_carries_same_key(session_file):
    """Копия несёт тот же ключ авторизации — иначе вход был бы потерян."""
    memory = _gateway(session_file, True)._session_arg()

    assert memory.auth_key is not None
    assert memory.auth_key.key == bytes(range(256))
    assert memory.dc_id == 2
    assert memory.server_address == "149.154.167.51"
    assert memory.port == 443


def test_detached_does_not_write_to_file(session_file):
    """Главное: копия не трогает файл, даже на запись метаданных."""
    before = session_file.stat().st_mtime_ns
    time.sleep(0.01)

    _gateway(session_file, True)._session_arg()

    assert session_file.stat().st_mtime_ns == before


def test_detached_works_while_file_is_locked(session_file):
    """Копия читается, даже когда файл держит другой процесс.

    Это и есть проверяемое поведение: раньше здесь падало с
    «database is locked».
    """
    holder = sqlite3.connect(str(session_file), timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE sessions SET port = 443")
    try:
        memory = _gateway(session_file, True)._session_arg()
        assert memory.auth_key is not None
    finally:
        holder.rollback()
        holder.close()


def test_missing_file_gives_empty_memory_session(tmp_path):
    """Сессии ещё нет — не падаем, отдаём пустую."""
    from telethon.sessions import MemorySession

    arg = _gateway(tmp_path / "нет.session", True)._session_arg()

    assert isinstance(arg, MemorySession)
    assert arg.auth_key is None


# --- режим процесса ----------------------------------------------------


def test_prefer_detached_switches_new_gateways(session_file, monkeypatch):
    """После объявления режима новые шлюзы создаются с копией сессии."""
    from app.services import secrets

    monkeypatch.setattr(secrets, "resolve", lambda key, default="": default)

    tg.prefer_detached(False)
    assert tg.legacy_gateway().detached is False

    tg.prefer_detached(True)
    assert tg.legacy_gateway().detached is True


def test_switching_mode_drops_cached_gateways(session_file, monkeypatch):
    """Переключение сбрасывает кэш: иначе процесс продолжил бы держать файл."""
    from app.services import secrets

    monkeypatch.setattr(secrets, "resolve", lambda key, default="": default)

    first = tg.legacy_gateway()
    tg.prefer_detached(True)
    second = tg.legacy_gateway()

    assert first is not second
    assert second.detached is True


def test_repeated_call_is_noop(session_file, monkeypatch):
    """Повторное объявление того же режима не сбрасывает кэш зря."""
    from app.services import secrets

    monkeypatch.setattr(secrets, "resolve", lambda key, default="": default)

    tg.prefer_detached(True)
    first = tg.legacy_gateway()
    tg.prefer_detached(True)

    assert tg.legacy_gateway() is first


def test_cli_command_switches_to_detached(monkeypatch, capsys):
    """Обычная команда CLI переходит на копию сессии.

    CLI запускают рядом с работающим воркером, который держит файл.
    """
    from app import cli

    tg.prefer_detached(False)
    assert cli.main(["gen-key"]) == 0
    capsys.readouterr()

    assert tg._prefer_detached is True


def test_cli_login_keeps_file_session(monkeypatch):
    """gift-cli login обязан писать в файл: он эту сессию и создаёт."""
    import app.cli as cli

    tg.prefer_detached(False)

    async def fake_login(_account):
        """Вход подменён: проверяем только режим сессии."""
        return 0

    monkeypatch.setattr(cli, "_login", fake_login)
    assert cli.main(["login"]) == 0

    assert tg._prefer_detached is False
