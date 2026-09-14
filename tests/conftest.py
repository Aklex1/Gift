"""Общая обвязка тестов: изолированная БД в памяти."""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("GIFT_SECRET_KEY", "test-secret-key-0123456789abcdef")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="gift-test-"))


@pytest.fixture()
def session():
    """Чистая сессия SQLite на каждый тест."""
    from sqlalchemy.orm import sessionmaker

    from app.db import build_engine
    from app.models import Base

    engine = build_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
