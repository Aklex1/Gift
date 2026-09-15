"""Тесты признака жизни сканера.

Бот слал «⚠️ Сканер молчит» каждые три часа при исправном воркере.
Причина: проверка была написана дважды — в панели и в уведомлениях, —
и версии разошлись. Панель мерила молчание по длительности прохода,
уведомления по интервалу запуска, а проход идёт дольше интервала:
полный обход занимает около четырёх минут при интервале в минуту.

Поэтому здесь проверяется не форматирование сообщения, а само правило:
признаком жизни считается **начало** прохода, а срок терпения выводится
из того, сколько проход занимает на самом деле.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models import utcnow
from app.services import runtime, scanner, store


@pytest.fixture(autouse=True)
def interval(session, monkeypatch):
    """Интервал запуска — минута, как на боевом сервере."""
    monkeypatch.setattr(runtime, "scan_interval", lambda: 60)
    store.invalidate()
    yield
    store.invalidate()


def _report(*, started_ago_sec: int, duration_sec: int = 0, running: bool = False):
    stamp = utcnow() - dt.timedelta(seconds=started_ago_sec)
    return {
        "started_at": stamp.isoformat(timespec="seconds"),
        "duration_sec": duration_sec,
        "running": running,
    }


def test_a_long_pass_is_not_silence():
    """Проход дольше интервала — это норма, а не сбой.

    Ровно этот случай и слал ложную тревогу: полный обход занимает
    четыре с лишним минуты, интервал — минута, и «пять интервалов»
    истекали, пока сканер честно работал.
    """
    state = scanner.liveness(_report(started_ago_sec=8 * 60, duration_sec=270))

    assert not state.stale
    assert state.budget_sec == 270 * 3


def test_a_dead_worker_is_still_caught():
    """Настоящее молчание тревогу не теряет."""
    state = scanner.liveness(_report(started_ago_sec=60 * 60, duration_sec=270))

    assert state.stale
    assert state.age_sec >= 3600


def test_start_is_the_sign_of_life_not_finish():
    """Пока проход идёт, воркер жив — даже если он ещё не закончил.

    Считать по finished_at значило бы объявлять сбоем каждый проход,
    который длится дольше срока терпения.
    """
    report = _report(started_ago_sec=30, duration_sec=270, running=True)
    report["finished_at"] = (
        utcnow() - dt.timedelta(hours=2)
    ).isoformat(timespec="seconds")

    assert not scanner.liveness(report).stale


def test_a_fast_pass_falls_back_to_the_interval():
    """Когда проход быстрый, расписание задаёт интервал."""
    state = scanner.liveness(_report(started_ago_sec=200, duration_sec=5))

    assert state.budget_sec == 180
    assert state.stale


def test_no_report_is_not_an_alarm():
    """Сканер, который ещё не ходил, не молчит — ему нечего сказать."""
    state = scanner.liveness({})

    assert not state.stale
    assert state.age_sec is None


def test_broken_timestamp_does_not_raise_an_alarm():
    """Испорченный отчёт — повод промолчать, а не звать чинить воркер."""
    assert not scanner.liveness({"started_at": "не дата"}).stale


def test_detail_names_the_numbers():
    """В тревоге названы обе цифры: сколько молчит и сколько ждали."""
    state = scanner.liveness(_report(started_ago_sec=3600, duration_sec=270))

    assert "60 мин" in state.detail
    assert "270 c" in state.detail
