"""Тесты хранилища ключей.

Главное, что проверяется: секрет не лежит в БД открытым текстом,
случайное пустое поле не стирает сохранённый токен, а значение из
панели перекрывает значение из .env.
"""

from __future__ import annotations

import pytest

from app.services import secrets


@pytest.fixture(autouse=True)
def isolated_db(session, monkeypatch):
    """Подменить БД приложения на тестовую и сбросить кэш."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(secrets, "session_scope", scope)
    secrets.invalidate()
    yield
    secrets.invalidate()


def test_secret_is_encrypted_at_rest(session):
    """Токен не хранится открытым текстом."""
    from app.models import Setting

    secrets.set_value("PORTALS_AUTH", "tma query_id=OPEN-SECRET")
    row = session.get(Setting, "PORTALS_AUTH")

    assert row is not None
    assert "OPEN-SECRET" not in (row.value or "")
    assert row.value.startswith("enc::")
    assert row.is_secret is True


def test_encrypted_value_reads_back(session):
    """Расшифровка возвращает исходное значение."""
    secrets.set_value("MRKT_AUTH", "eyJhbGciOiJIUzI1NiJ9.payload")
    assert secrets.get("MRKT_AUTH") == "eyJhbGciOiJIUzI1NiJ9.payload"


def test_non_secret_stored_plainly(session):
    """Несекретные поля хранятся как есть — их удобно править."""
    from app.models import Setting

    secrets.set_value("OWNER_IDS", "123,456")
    row = session.get(Setting, "OWNER_IDS")
    assert row.value == "123,456"
    assert row.is_secret is False


def test_panel_value_beats_env(session):
    """Значение из панели перекрывает .env."""
    assert secrets.resolve("PORTALS_AUTH", "из-env") == "из-env"
    secrets.set_value("PORTALS_AUTH", "из-панели")
    assert secrets.resolve("PORTALS_AUTH", "из-env") == "из-панели"


def test_clearing_falls_back_to_env(session):
    """После очистки снова работает значение из .env."""
    secrets.set_value("PORTALS_AUTH", "из-панели")
    secrets.set_value("PORTALS_AUTH", "")
    assert secrets.resolve("PORTALS_AUTH", "из-env") == "из-env"
    assert secrets.get("PORTALS_AUTH") is None


def test_blank_write_does_not_leak_empty_value(session):
    """Запись пустой строки трактуется как очистка, а не как пустой токен."""
    secrets.set_value("TONNEL_AUTH", "   ")
    assert secrets.get("TONNEL_AUTH") is None


def test_audit_records_mask_not_value(session):
    """В журнал попадает маска, а не сам секрет."""
    from app.models import AuditLog

    secrets.set_value("BOT_TOKEN", "7123456789:AAFverysecretvalue")
    entry = (
        session.query(AuditLog)
        .filter(AuditLog.target == "BOT_TOKEN")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None
    dumped = str(entry.payload)
    assert "verysecretvalue" not in dumped
    assert "*" in dumped


def test_masked_state_hides_secrets(session):
    """Интерфейс не получает секрет целиком."""
    secrets.set_value("PORTALS_AUTH", "tma query_id=TOPSECRETVALUE")
    state = secrets.masked_state()["PORTALS_AUTH"]

    assert state["is_set"] is True
    assert "TOPSECRETVALUE" not in state["display"]
    assert state["source"] == "панель"


def test_masked_state_shows_plain_for_public_fields(session):
    """Несекретные поля показываются целиком — их надо видеть и править."""
    secrets.set_value("OWNER_IDS", "123456789")
    state = secrets.masked_state()["OWNER_IDS"]
    assert state["display"] == "123456789"


def test_every_field_has_hint():
    """У каждого поля есть подсказка, где взять значение."""
    for field in secrets.FIELDS:
        assert field.hint, f"{field.key}: нет подсказки"
        assert field.group, f"{field.key}: нет группы"


def test_unreadable_secret_does_not_crash(session, monkeypatch):
    """Смена ключа шифрования не должна ронять приложение.

    Один нечитаемый секрет обязан деградировать до «не задано»,
    иначе бот перестаёт запускаться целиком.
    """
    from app.models import Setting

    session.add(
        Setting(key="PORTALS_AUTH", value="enc::поврежденные-данные", is_secret=True)
    )
    session.flush()
    secrets.invalidate()

    # Не бросает исключение и откатывается на .env.
    assert secrets.get("PORTALS_AUTH") is None
    assert secrets.resolve("PORTALS_AUTH", "из-env") == "из-env"


def test_masked_state_survives_unreadable_secret(session):
    """Страница настроек открывается даже с повреждённым секретом."""
    from app.models import Setting

    session.add(
        Setting(key="MRKT_AUTH", value="enc::мусор", is_secret=True)
    )
    session.flush()
    secrets.invalidate()

    state = secrets.masked_state()
    assert state["MRKT_AUTH"]["is_set"] is False
