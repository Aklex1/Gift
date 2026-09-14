"""Тест дополнения .env новыми настройками.

Главное требование: обновление не должно трогать уже заданные
значения — иначе оно затрёт ключ шифрования или токен бота.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def env_files(tmp_path, monkeypatch):
    """Подменить каталог проекта на временный."""
    from app import config

    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    return tmp_path


def _run(tmp_path) -> int:
    from app.cli import cmd_env_sync

    return cmd_env_sync()


def test_existing_values_are_never_touched(env_files, capsys):
    """Заданные значения остаются как были."""
    (env_files / ".env.example").write_text(
        "GIFT_SECRET_KEY=\nBOT_TOKEN=\nNEW_SETTING=default\n", encoding="utf-8"
    )
    (env_files / ".env").write_text(
        "GIFT_SECRET_KEY=мой-ключ\nBOT_TOKEN=7123:секрет\n", encoding="utf-8"
    )

    assert _run(env_files) == 0

    result = (env_files / ".env").read_text(encoding="utf-8")
    assert "GIFT_SECRET_KEY=мой-ключ" in result
    assert "BOT_TOKEN=7123:секрет" in result
    assert "NEW_SETTING=default" in result


def test_missing_setting_is_added_once(env_files):
    """Повторный запуск не создаёт дубликатов."""
    (env_files / ".env.example").write_text(
        "PORTALS_ENABLE_WRITE=false\n", encoding="utf-8"
    )
    (env_files / ".env").write_text("DEFAULT_MODE=safe\n", encoding="utf-8")

    _run(env_files)
    _run(env_files)

    result = (env_files / ".env").read_text(encoding="utf-8")
    assert result.count("PORTALS_ENABLE_WRITE=") == 1


def test_comments_come_along(env_files):
    """Пояснения к настройке переносятся вместе с ней."""
    (env_files / ".env.example").write_text(
        "# Потолок одной сделки в TON\nPORTALS_MAX_TRADE_TON=0\n", encoding="utf-8"
    )
    (env_files / ".env").write_text("DEFAULT_MODE=safe\n", encoding="utf-8")

    _run(env_files)

    result = (env_files / ".env").read_text(encoding="utf-8")
    assert "# Потолок одной сделки в TON" in result


def test_missing_env_is_created_from_example(env_files):
    """Если .env нет вовсе, он создаётся из шаблона."""
    (env_files / ".env.example").write_text("DEFAULT_MODE=safe\n", encoding="utf-8")

    assert _run(env_files) == 0
    assert (env_files / ".env").read_text(encoding="utf-8") == "DEFAULT_MODE=safe\n"


def test_obsolete_value_is_replaced(env_files):
    """Нерабочий адрес площадки заменяется на актуальный.

    Существующие значения мы не трогаем принципиально, но мёртвый
    домен иначе остался бы в файле навсегда и площадка не работала бы.
    """
    (env_files / ".env.example").write_text(
        "PORTALS_BASE_URL=https://portals.tg/api\n", encoding="utf-8"
    )
    (env_files / ".env").write_text(
        "GIFT_SECRET_KEY=мой-ключ\n"
        "PORTALS_BASE_URL=https://portals-market.com/api\n",
        encoding="utf-8",
    )

    _run(env_files)

    result = (env_files / ".env").read_text(encoding="utf-8")
    assert "portals.tg/api" in result
    assert "portals-market.com" not in result
    # Секрет остался нетронутым.
    assert "GIFT_SECRET_KEY=мой-ключ" in result


def test_custom_value_is_not_replaced(env_files):
    """Свой адрес пользователя не перезаписывается."""
    (env_files / ".env.example").write_text(
        "PORTALS_BASE_URL=https://portals.tg/api\n", encoding="utf-8"
    )
    (env_files / ".env").write_text(
        "PORTALS_BASE_URL=https://my-proxy.local/api\n", encoding="utf-8"
    )

    _run(env_files)

    result = (env_files / ".env").read_text(encoding="utf-8")
    assert "my-proxy.local" in result
