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
from app.services import runtime
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


async def task_fx() -> None:
    """Обновить курсы валют из внешних источников."""

    async def run() -> None:
        from app.services import fx

        await fx.refresh()

    await _guarded("fx", run)


async def task_alerts() -> None:
    """Проверить, о чём стоит предупредить владельца."""

    async def run() -> None:
        from app.services import notify

        await notify.check_all()

    await _guarded("alerts", run)


async def task_tokens() -> None:
    """Продлить токены площадок до того, как они протухнут.

    Токен площадки — это initData мини-приложения, он живёт часы.
    Раньше протухший токен означал остановку торговли до тех пор, пока
    человек не принесёт новую строку из DevTools.
    """

    async def run() -> None:
        from app.services import notify, webauth

        for report in await webauth.renew_all():
            if report.get("skipped"):
                continue
            market = report["market"]
            if report["ok"]:
                log.info("Токен %s: %s", market, report["detail"])
                await notify.resolved(
                    "token", market, f"Токен {market} снова продлевается."
                )
                continue

            log.warning("Токен %s: %s", market, report["detail"])
            # Молчать нельзя: без токена площадка выпадает из торговли,
            # и заметить это по пустым результатам сканера трудно.
            await notify.alert(
                "token",
                market,
                f"Не удалось продлить токен {market}: {report['detail']}\n"
                f"Торговля на этой площадке остановится, когда истечёт "
                f"текущий токен. Впишите его вручную в «Настройки».",
            )

    await _guarded("tokens", run)


async def task_feed() -> None:
    """Прочитать канал находок и обновить коллекции стратегии.

    Канал показывает, в каких коллекциях недооценённые лоты
    появляются на практике. Сканер ограничен по пропускной
    способности, и сузить его до десятка коллекций — значит
    осматривать каждую за секунды, а не раз в час.
    """

    async def run() -> None:
        from app.services import feed, strategy as strategy_service

        if not feed.channel_ref():
            return

        report = await feed.sync()
        if report.get("error"):
            return

        with session_scope() as session:
            target = strategy_service.feed_strategy(session)
            if target is None or not target.is_enabled:
                return
            result = strategy_service.refresh_feed_collections(session)

        if result.get("ok"):
            log.info(
                "Коллекции канала: %s (потолок %s Stars)",
                ", ".join(result["collections"]),
                result.get("max_price_stars") or "—",
            )

    await _guarded("feed", run)


async def task_maintenance() -> None:
    """Освободить протухшие резервы и кандидатов."""

    async def run() -> None:
        with session_scope() as session:
            freed = budget_service.expire_stale(session)
            expired = scanner.expire_candidates(session)
        if freed or expired:
            log.info("Обслуживание: резервов %s, кандидатов %s", freed, expired)

    await _guarded("maintenance", run)


def apply_scan_interval(scheduler) -> None:
    """Привести расписание скана к текущей настройке.

    Значение меняют из панели — в другом процессе, — поэтому проверяем
    его периодически и пересобираем задание, когда оно разошлось с
    расписанием.
    """
    wanted = runtime.scan_interval()
    job = scheduler.get_job("scan")
    if job is None:
        return
    current = getattr(job.trigger, "interval", None)
    current_sec = int(current.total_seconds()) if current else None
    if current_sec == wanted:
        return

    scheduler.reschedule_job("scan", trigger="interval", seconds=wanted)
    log.info("Интервал сканирования изменён: %s c -> %s c", current_sec, wanted)


async def main() -> None:
    """Запустить планировщик."""
    setup_logging("worker")
    init_db()
    sync_capabilities()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        task_scan, "interval", seconds=runtime.scan_interval(), id="scan"
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
    scheduler.add_job(task_fx, "interval", seconds=900, id="fx")
    scheduler.add_job(task_alerts, "interval", seconds=300, id="alerts")
    # Проверяем возраст токенов чаще, чем они живут: сама проверка
    # дешёвая, запрос к Telegram уходит только при реальной надобности.
    scheduler.add_job(task_tokens, "interval", seconds=1800, id="tokens")
    # Канал публикует раз в сутки — чаще получаса смотреть незачем.
    scheduler.add_job(task_feed, "interval", seconds=1800, id="feed")
    scheduler.add_job(task_maintenance, "interval", seconds=60, id="maintenance")
    # Интервал сканирования меняют из панели, а планировщику он задан
    # при запуске. Без пересборки задания настройка молча не работала
    # бы до перезапуска воркера.
    scheduler.add_job(
        lambda: apply_scan_interval(scheduler),
        "interval",
        seconds=30,
        id="scan_interval",
    )
    scheduler.start()

    # Курсы нужны сразу: без них первые же расчёты будут приблизительными.
    await task_fx()
    # И токены: стартовать с протухшим — значит потерять первые минуты.
    await task_tokens()

    log.info(
        "Воркеры запущены: скан %s c, репрайс %s c, сверка %s c",
        runtime.scan_interval(),
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
