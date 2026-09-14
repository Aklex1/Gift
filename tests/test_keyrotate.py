"""Тесты смены ключа шифрования.

Главное, что проверяется: после смены ключа секреты читаются новым
ключом и не читаются старым, а при негодном старом ключе база остаётся
нетронутой — частично перешифрованная база хуже исходной.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy.orm import sessionmaker

from app.crypto import _PREFIX
from app.models import Account, AuditLog, Base, Setting
from app.services import keyrotate

OLD_KEY = "old-key-for-rotation-tests-0123456789"
NEW_KEY = "new-key-for-rotation-tests-9876543210"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Настоящая транзакционная БД на файле: нужен реальный откат."""
    from app.db import build_engine

    engine = build_engine(f"sqlite:///{tmp_path / 'rotate.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def scope():
        """Копия app.db.session_scope, но на тестовом движке."""
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(keyrotate, "session_scope", scope)
    yield scope
    engine.dispose()


def _enc(key: str, plain: str) -> str:
    """Зашифровать значение конкретным ключом."""
    return _PREFIX + keyrotate._fernet_for(key).encrypt(plain.encode()).decode()


@pytest.fixture()
def filled(db):
    """Два секрета и аккаунт, зашифрованные старым ключом."""
    with db() as s:
        s.add(Setting(key="PORTALS_AUTH", value=_enc(OLD_KEY, "tma token"), is_secret=True))
        s.add(Setting(key="MRKT_AUTH", value=_enc(OLD_KEY, "mrkt token"), is_secret=True))
        s.add(Setting(key="MIN_ROI", value="0.12", is_secret=False))
        s.add(
            Account(
                name="основной",
                api_id=123,
                api_hash_enc=_enc(OLD_KEY, "hash-value"),
                session_name="main",
            )
        )
    return db


def test_rotate_reencrypts_everything(filled):
    """После смены ключа всё читается новым ключом и не читается старым."""
    report = keyrotate.rotate(OLD_KEY, NEW_KEY)

    assert report == {"settings": 2, "accounts": 1}
    assert keyrotate.verify(NEW_KEY) == {"ok": 3, "failed": []}
    assert len(keyrotate.verify(OLD_KEY)["failed"]) == 3


def test_plaintext_survives_untouched(filled):
    """Несекретные настройки ротация не трогает."""
    keyrotate.rotate(OLD_KEY, NEW_KEY)

    with filled() as s:
        assert s.get(Setting, "MIN_ROI").value == "0.12"


def test_values_are_preserved(filled):
    """Перешифрованный секрет расшифровывается в исходный текст."""
    keyrotate.rotate(OLD_KEY, NEW_KEY)

    with filled() as s:
        stored = s.get(Setting, "PORTALS_AUTH").value
    plain = keyrotate._fernet_for(NEW_KEY).decrypt(stored[len(_PREFIX) :].encode())
    assert plain.decode() == "tma token"


def test_wrong_old_key_changes_nothing(filled):
    """Негодный старый ключ отменяет операцию целиком."""
    with filled() as s:
        before = {r.key: r.value for r in s.query(Setting).all()}
        before_hash = s.query(Account).one().api_hash_enc

    with pytest.raises(ValueError, match="Старый ключ не подходит"):
        keyrotate.rotate("совсем-другой-ключ-1234567890", NEW_KEY)

    with filled() as s:
        after = {r.key: r.value for r in s.query(Setting).all()}
        after_hash = s.query(Account).one().api_hash_enc

    assert after == before
    assert after_hash == before_hash
    # И ни одной записи в аудите: ротации не было.
    with filled() as s:
        assert s.query(AuditLog).count() == 0


def test_same_key_rejected(filled):
    """Смена ключа на тот же самый — ошибка, а не пустая работа."""
    with pytest.raises(ValueError, match="совпадает"):
        keyrotate.rotate(OLD_KEY, OLD_KEY)


def test_empty_new_key_rejected(filled):
    """Пустой новый ключ не принимается."""
    with pytest.raises(ValueError, match="негоден"):
        keyrotate.rotate(OLD_KEY, "   ")


def test_rotation_is_audited(filled):
    """Смена ключа попадает в аудит."""
    keyrotate.rotate(OLD_KEY, NEW_KEY, actor="tester")

    with filled() as s:
        entry = s.query(AuditLog).one()
    assert entry.action == "security.key_rotated"
    assert entry.actor == "tester"
    assert entry.payload == {"settings": 2, "accounts": 1}


def test_plan_lists_targets_without_changing(filled):
    """План показывает состав работы и ничего не меняет."""
    overview = keyrotate.plan(NEW_KEY)

    assert overview["settings"] == ["MRKT_AUTH", "PORTALS_AUTH"]
    assert overview["accounts"] == ["основной"]
    assert overview["new_key_valid"] is True
    assert keyrotate.verify(OLD_KEY)["ok"] == 3


def test_verify_on_empty_db(db):
    """Пустая база — не ошибка, просто нечего проверять."""
    assert keyrotate.verify(NEW_KEY) == {"ok": 0, "failed": []}


def test_env_key_read_and_write(tmp_path):
    """Ключ в .env заменяется, остальной файл не страдает."""
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgresql://x\nGIFT_SECRET_KEY=старый\nMIN_ROI=0.1\n",
        encoding="utf-8",
    )

    assert keyrotate.read_env_key(env) == "старый"
    assert keyrotate.write_env_key(env, "новый") is True

    text = env.read_text(encoding="utf-8")
    assert "GIFT_SECRET_KEY=новый" in text
    assert "DATABASE_URL=postgresql://x" in text
    assert "MIN_ROI=0.1" in text
    assert keyrotate.read_env_key(env) == "новый"


def test_env_write_reports_missing_line(tmp_path):
    """Если строки с ключом нет — честный False, а не тихий успех."""
    env = tmp_path / ".env"
    env.write_text("MIN_ROI=0.1\n", encoding="utf-8")

    assert keyrotate.read_env_key(env) == ""
    assert keyrotate.write_env_key(env, "новый") is False
