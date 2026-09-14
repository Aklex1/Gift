"""Командная строка: установка, логин, диагностика.

Команды:
    gen-key     — сгенерировать ключ шифрования секретов
    init        — создать схему БД и стартовые данные
    login       — интерактивная авторизация торгового аккаунта Telegram
    whoami      — показать, под каким аккаунтом работает сессия
    balance     — балансы Stars/TON
    probe       — живая проверка доступности площадок
    scan        — разовый проход сканера
    inventory   — сверка портфеля с инвентарём
    doctor      — проверка конфигурации перед запуском
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from app.config import settings


def cmd_gen_key() -> int:
    """Сгенерировать ключ шифрования."""
    from app.crypto import generate_key

    print(generate_key())
    return 0


def cmd_init() -> int:
    """Создать схему БД и стартовые данные."""
    from app.adapters.registry import sync_capabilities
    from app.db import init_db, session_scope
    from app.services.strategy import seed_default_strategy
    from app.services.valuation import seed_fee_schedules

    init_db()
    with session_scope() as session:
        seed_fee_schedules(session)
        seed_default_strategy(session)
    sync_capabilities()
    print("✓ База данных готова, стартовая стратегия создана (выключена, режим SAFE)")
    return 0


async def _login() -> int:
    """Интерактивная авторизация Telethon."""
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    if not settings.tg_api_id or not settings.tg_api_hash:
        print(
            "✗ TG_API_ID / TG_API_HASH не заданы.\n"
            "  Получите их на https://my.telegram.org -> API development tools\n"
            "  и впишите в файл .env",
            file=sys.stderr,
        )
        return 1

    settings.ensure_dirs()
    client = TelegramClient(
        str(settings.session_path.with_suffix("")),
        settings.tg_api_id,
        settings.tg_api_hash,
    )
    await client.connect()

    if not await client.is_user_authorized():
        phone = settings.tg_phone or input("Номер телефона (в формате +7…): ").strip()
        await client.send_code_request(phone)
        code = input("Код из Telegram: ").strip()
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            password = input("Пароль двухфакторной защиты: ").strip()
            await client.sign_in(password=password)

    me = await client.get_me()
    print(
        f"✓ Авторизован: {getattr(me, 'first_name', '')} "
        f"(@{getattr(me, 'username', '—')}, id={getattr(me, 'id', '?')})"
    )
    print(f"  Файл сессии: {settings.session_path}")
    await client.disconnect()
    return 0


async def _whoami() -> int:
    """Показать текущий торговый аккаунт."""
    from app.adapters.telegram_gateway import gateway

    me = await gateway.me()
    print(
        f"Аккаунт: {getattr(me, 'first_name', '')} "
        f"(@{getattr(me, 'username', '—')}, id={getattr(me, 'id', '?')})"
    )
    await gateway.close()
    return 0


async def _balance() -> int:
    """Показать балансы."""
    from app.adapters.registry import get_adapter
    from app.adapters.ton import TonClient
    from app.enums import Market

    adapter = get_adapter(Market.TELEGRAM)
    for item in await adapter.balance():
        print(f"Telegram · {item.currency.value}: {item.amount}")

    if settings.ton_wallet_address:
        ton = TonClient()
        try:
            print(f"TON-кошелёк: {await ton.balance()} TON")
        finally:
            await ton.close()
    return 0


async def _probe() -> int:
    """Проверить доступность площадок."""
    import json

    from app.adapters.registry import probe_all
    from app.db import init_db

    init_db()
    report = await probe_all()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


async def _scan() -> int:
    """Разовый проход сканера."""
    from app.db import init_db
    from app.services.scanner import scan_once

    init_db()
    report = await scan_once()
    print(
        f"Лотов: {report['listings']}\n"
        f"Фактов продаж: {report['facts']}\n"
        f"Кандидатов: {report['candidates']}"
    )
    for market, count in (report.get("markets") or {}).items():
        print(f"  {market}: {count}")
    return 0


async def _inventory() -> int:
    """Сверка портфеля с инвентарём Telegram."""
    from app.db import init_db
    from app.enums import Market
    from app.services.portfolio import sync_inventory

    init_db()
    print(await sync_inventory(Market.TELEGRAM))
    return 0


def cmd_doctor() -> int:
    """Проверить конфигурацию перед запуском."""
    problems: list[str] = []
    warnings: list[str] = []

    print("=== Проверка конфигурации ===\n")

    # Секреты
    if not settings.secret_key:
        problems.append("GIFT_SECRET_KEY не задан (python -m app.cli gen-key)")
    if not settings.bot_token:
        problems.append("BOT_TOKEN не задан (получите у @BotFather)")
    if not settings.owner_id_list:
        problems.append(
            "OWNER_IDS пуст — бот не ответит никому "
            "(узнайте свой id командой /id в боте)"
        )
    if not settings.tg_api_id or not settings.tg_api_hash:
        problems.append(
            "TG_API_ID / TG_API_HASH не заданы — поиск подарков работать не будет "
            "(https://my.telegram.org)"
        )
    elif not settings.session_path.exists():
        problems.append(
            f"Сессия не создана: {settings.session_path} "
            "(выполните: gift-cli login)"
        )
    if not settings.web_password:
        warnings.append("WEB_PASSWORD не задан — веб-панель отключена")

    # БД
    try:
        from app.db import engine

        with engine.connect():
            pass
        print(f"✓ БД доступна: {engine.url.render_as_string(hide_password=True)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"БД недоступна: {exc}")

    # Предохранители
    print(f"  Режим: {settings.default_mode}")
    print(f"  Аварийный стоп: {'ВКЛЮЧЁН' if settings.kill_switch else 'выключен'}")
    print(f"  Лимит на сделку: {settings.max_trade_stars or 'не задан'} Stars")
    print(f"  Суточный лимит: {settings.daily_limit_stars or 'не задан'} Stars")
    print(f"  Белый список AUTO: {', '.join(settings.auto_markets) or 'пуст'}")

    if Decimal(settings.max_trade_stars or 0) == 0:
        warnings.append(
            "MAX_TRADE_STARS = 0: нет предела на одну сделку. "
            "Установите значение перед включением SEMI/AUTO."
        )
    if settings.auto_markets - {"telegram"}:
        problems.append(
            "В AUTO_WHITELIST есть площадки, кроме telegram. "
            "Приватные API без SLA не допускаются в автономный режим."
        )

    print()
    for item in warnings:
        print(f"⚠ {item}")
    for item in problems:
        print(f"✗ {item}")
    if not problems:
        print("\n✓ Критичных проблем нет.")
        return 0
    print(f"\nПроблем: {len(problems)}")
    return 1


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(
        prog="gift-cli", description="Управление торговым ботом подарков Telegram"
    )
    parser.add_argument(
        "command",
        choices=[
            "gen-key",
            "init",
            "login",
            "whoami",
            "balance",
            "probe",
            "scan",
            "inventory",
            "doctor",
        ],
    )
    args = parser.parse_args(argv)

    sync_commands = {"gen-key": cmd_gen_key, "init": cmd_init, "doctor": cmd_doctor}
    if args.command in sync_commands:
        return sync_commands[args.command]()

    async_commands = {
        "login": _login,
        "whoami": _whoami,
        "balance": _balance,
        "probe": _probe,
        "scan": _scan,
        "inventory": _inventory,
    }
    return asyncio.run(async_commands[args.command]())


if __name__ == "__main__":
    raise SystemExit(main())
