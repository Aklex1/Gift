"""Подключение к БД и сессии.

Поддерживаются PostgreSQL (целевой runtime) и SQLite (локальные тесты).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.models import Base

log = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    """Привести URL к драйверу, который реально установлен."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def build_engine(url: str | None = None) -> Engine:
    """Создать engine с настройками под конкретный диалект."""
    url = _normalize_url(url or settings.database_url)
    kwargs: dict = {"pool_pre_ping": True, "future": True}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            """WAL и внешние ключи для SQLite."""
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


engine: Engine = build_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакционная сессия: commit при успехе, rollback при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_columns() -> list[str]:
    """Добавить недостающие колонки в уже существующие таблицы.

    ``create_all`` создаёт новые таблицы, но не меняет старые. При
    обновлении версии на работающем сервере новые поля иначе остались
    бы отсутствующими, и запросы падали бы.

    Делается только добавление колонок — существующие данные не
    трогаются, ничего не удаляется.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                sql = (
                    f'ALTER TABLE "{table.name}" '
                    f'ADD COLUMN "{column.name}" {col_type}'
                )
                # NOT NULL без значения по умолчанию на непустой таблице
                # добавить нельзя — такие колонки заводим допускающими NULL.
                conn.execute(text(sql))
                added.append(f"{table.name}.{column.name}")
                log.info("Добавлена колонка %s.%s", table.name, column.name)
    return added


def init_db() -> None:
    """Создать схему и догнать её до текущей версии моделей."""
    settings.ensure_dirs()
    Base.metadata.create_all(engine)
    added = sync_columns()
    if added:
        log.info("Схема обновлена, новых колонок: %s", len(added))
    log.info("Схема БД готова: %s", engine.url.render_as_string(hide_password=True))


def is_postgres() -> bool:
    """PostgreSQL ли под нами (нужно для SELECT ... FOR UPDATE)."""
    return engine.dialect.name == "postgresql"
