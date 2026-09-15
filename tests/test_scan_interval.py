"""Тесты настройки интервала сканирования.

Интервал задаётся планировщику при запуске воркера, а меняют его из
панели — из другого процесса. Поэтому проверяется не только хранение,
но и то, что изменение доходит до расписания без перезапуска: иначе
настройка молча не работала бы.
"""

from __future__ import annotations

import pytest

from app.services import runtime, store


@pytest.fixture(autouse=True)
def isolated(session, monkeypatch):
    """Изолированное хранилище настроек."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(store, "session_scope", scope)
    store.invalidate()
    yield
    store.invalidate()


# --- хранение и границы ------------------------------------------------


def test_default_from_env():
    """Без настройки берётся значение из конфига."""
    from app.config import settings

    assert runtime.scan_interval() == int(settings.scan_interval_sec)


def test_value_persists():
    """Сохранённое значение читается обратно."""
    runtime.set_scan_interval(300)
    store.invalidate()

    assert runtime.scan_interval() == 300


def test_too_small_clamped():
    """Слишком частый опрос подрезается: площадки ответят лимитами."""
    assert runtime.set_scan_interval(1) == runtime.SCAN_INTERVAL_MIN


def test_too_large_clamped():
    """Слишком редкий — тоже: находки успеют устареть."""
    assert runtime.set_scan_interval(999_999) == runtime.SCAN_INTERVAL_MAX


def test_garbage_falls_back_to_default():
    """Мусор в хранилище не ломает воркер."""
    from app.config import settings

    store.set(runtime.KEY_SCAN_INTERVAL, "не число")
    store.invalidate()

    assert runtime.scan_interval() == int(settings.scan_interval_sec)


# --- подхват расписанием ----------------------------------------------


def _scheduler():
    """Планировщик с заданием скана."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.workers.runner import task_scan

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        task_scan, "interval", seconds=runtime.scan_interval(), id="scan"
    )
    return scheduler


def _current(scheduler) -> int:
    """Интервал, который сейчас в расписании."""
    return int(scheduler.get_job("scan").trigger.interval.total_seconds())


def test_schedule_follows_setting():
    """Изменение настройки доходит до расписания без перезапуска."""
    from app.workers.runner import apply_scan_interval

    scheduler = _scheduler()
    assert _current(scheduler) == 60

    runtime.set_scan_interval(300)
    store.invalidate()
    apply_scan_interval(scheduler)

    assert _current(scheduler) == 300


def test_unchanged_setting_leaves_schedule_alone():
    """Без изменений расписание не трогается зря.

    Пересборка сдвигает следующий запуск, и делать её на каждой
    проверке значило бы откладывать скан каждые полминуты — то есть
    не сканировать вовсе.
    """
    from app.workers.runner import apply_scan_interval

    scheduler = _scheduler()
    # Пересборка создаёт новый триггер, поэтому сравниваем сам объект.
    before = scheduler.get_job("scan").trigger

    apply_scan_interval(scheduler)

    assert scheduler.get_job("scan").trigger is before


def test_missing_job_is_safe():
    """Если задания нет, проверка не падает."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.workers.runner import apply_scan_interval

    apply_scan_interval(AsyncIOScheduler(timezone="UTC"))


def test_clamped_value_reaches_schedule():
    """В расписание попадает подрезанное значение, а не запрошенное."""
    from app.workers.runner import apply_scan_interval

    scheduler = _scheduler()
    runtime.set_scan_interval(5)
    store.invalidate()
    apply_scan_interval(scheduler)

    assert _current(scheduler) == runtime.SCAN_INTERVAL_MIN
