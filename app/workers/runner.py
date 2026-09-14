"""Планировщик фоновых задач.

Задачи:
    scan       — поиск возможностей на рынках
    reprice    — выставление и снижение цен
    reconcile  — сверка неизвестных исходов
    inventory  — сверка портфеля с инвентарём площадки
    maintenance— освобождение протухших резервов и кандидатов

Все задачи идемпотентны: повторный запуск ничего не ломает.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.adapters.registry import close_all, sync_capabilities
from app.config import settings
from app.db import init_db, session_scope
from app.enums import Market
from app.logging_conf import setup_logging
from app.services import budget as budget_service
from app.services import portfolio, reconciler, repricer, scanner

log = logging.getLogger(__name__)

#: Защита от наложения: задача не запускается, пока предыдущая идёт.
_running: set[str] = set()


async def _guarded(name: str, coro_factory) -> None:
    """Выполнить задачу, не допуская параллельного запуска той же задачи."""
    if name in _running:
        log.debug("Задача %s ещё выполняется — пропускаю тик", name)
        return
    _running.add(name)
    try:
        await coro_factory()
    except Exception as exc:  # noqa: BLE001 - воркер не должен падать
        log.exception("Задача %s завершилась ошибкой: %s", name, exc)
    finally:
        _running.discard(name)


async def task_scan() -> None:
    """Проход сканера рынков.

    Ошибку прохода надо не только записать в журнал, но и показать в
    панели: иначе там останется висеть старый отчёт, и по нему будет
    казаться, что сканер просто ничего не нашёл.
    """

    async def run() -> None:
        try:
            await scanner.scan_once()
        except Exception as exc:  # noqa: BLE001 - сообщаем и продолжаем
            scanner.save_failure(exc)
            raise

    await _guarded("scan", run)


async def task_reprice() -> None:
    """Проход репрайсера."""
    await _guarded("reprice", repricer.run_once)


async def task_reconcile() -> None:
    """Сверка неизвестных исходов."""
    await _guarded("reconcile", reconciler.run_once)


async def task_inventory() -> None:
    """Сверка портфеля с инвентарём Telegram."""

    async def run() -> None:
        await portfolio.sync_inventory(Market.TELEGRAM)

    await _guarded("inventory", run)


async def task_balances() -> None:
    """Обновить балансы аккаунтов и кошельков площадок."""

    async def run() -> None:
        from app.services import accounts as accounts_service
        from app.services import balances

        await accounts_service.refresh_balances()
        await balances.refresh()

    await _guarded("balances", run)


async def task_maintenance() -> None:
    """Освободить протухшие резервы и кандидатов."""

    async def run() -> None:
        with session_scope() as session:
            freed = budget_service.expire_stale(session)
            expired = scanner.expire_candidates(session)
        if freed or expired:
            log.info("Обслуживание: резервов %s, кандидатов %s", freed, expired)

    await _guarded("maintenance", run)


async def main() -> None:
    """Запустить планировщик."""
    setup_logging("worker")
    init_db()
    sync_capabilities()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        task_scan, "interval", seconds=settings.scan_interval_sec, id="scan"
    )
    scheduler.add_job(
        task_reprice, "interval", seconds=settings.reprice_interval_sec, id="reprice"
    )
    scheduler.add_job(
        task_reconcile,
        "interval",
        seconds=settings.reconcile_interval_sec,
        id="reconcile",
    )
    scheduler.add_job(task_inventory, "interval", seconds=600, id="inventory")
    scheduler.add_job(task_balances, "interval", seconds=300, id="balances")
    scheduler.add_job(task_maintenance, "interval", seconds=60, id="maintenance")
    scheduler.start()

    log.info(
        "Воркеры запущены: скан %s c, репрайс %s c, сверка %s c",
        settings.scan_interval_sec,
        settings.reprice_interval_sec,
        settings.reconcile_interval_sec,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    log.info("Останавливаю воркеры…")
    scheduler.shutdown(wait=False)
    await close_all()


if __name__ == "__main__":
    asyncio.run(main())
