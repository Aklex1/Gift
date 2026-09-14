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
    contract    — работа с боевым контрактом площадки:
                  `contract portals` показывает состояние,
                  `contract portals --template` создаёт заготовку
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

    from app.adapters.telegram_gateway import gateway

    api_id, api_hash = gateway.credentials()
    if not api_id or not api_hash:
        print(
            "✗ api_id / api_hash не заданы.\n"
            "  Получите их на https://my.telegram.org -> API development tools\n"
            "  и укажите в веб-панели (Настройки) либо в файле .env",
            file=sys.stderr,
        )
        return 1

    settings.ensure_dirs()
    client = TelegramClient(
        str(settings.session_path.with_suffix("")), api_id, api_hash
    )
    await client.connect()

    if not await client.is_user_authorized():
        from app.services import secrets

        phone = secrets.resolve("TG_PHONE", settings.tg_phone) or input(
            "Номер телефона (в формате +7…): "
        ).strip()
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


async def _contract(market_name: str, make_template: bool) -> int:
    """Проверить готовность площадки к боевому режиму.

    Ничего не покупает: только читает каталог, инвентарь и проверяет,
    описаны ли write-эндпоинты.
    """
    from app.adapters.base import Capability, CapabilityStatus
    from app.adapters.contracts import (
        WRITE_OPS,
        contract_path,
        is_placeholder,
        load,
        write_template,
    )
    from app.adapters.registry import get_adapter
    from app.config import settings as cfg
    from app.enums import Market

    try:
        market = Market(market_name)
    except ValueError:
        print(f"✗ Неизвестная площадка: {market_name}", file=sys.stderr)
        print(f"  Доступные: {', '.join(m.value for m in Market)}", file=sys.stderr)
        return 1

    if make_template:
        path = write_template(market.value)
        print(f"✓ Заготовка контракта: {path}")
        print("  Заполните пути по реальным запросам мини-приложения")
        print("  (DevTools -> Network) и запустите проверку снова.")
        return 0

    print(f"=== Готовность {market.value} к боевому режиму ===\n")
    problems = 0

    # 1. Флаг площадки
    write_on = cfg.market_write_enabled(market.value)
    print(f"{'✓' if write_on else '✗'} Боевой режим: "
          f"{market.value.upper()}_ENABLE_WRITE = {str(write_on).lower()}")
    if not write_on:
        problems += 1

    # 2. Контракт
    path = contract_path(market.value)
    contract = load(market.value)
    if not path.exists():
        print(f"✗ Контракт не создан: {path}")
        print(f"  Создайте заготовку: gift-cli contract {market.value} --template")
        problems += 1
    elif is_placeholder(market.value):
        print(f"✗ Контракт не заполнен (остались заглушки ЗАПОЛНИТЕ): {path}")
        problems += 1
    else:
        described = contract.described
        print(f"✓ Контракт: {path}")
        for op in WRITE_OPS:
            mark = "✓" if op in described else "·"
            endpoint = contract.get(op)
            detail = f"{endpoint.method} {endpoint.path}" if endpoint else "не описана"
            print(f"    {mark} {op}: {detail}")
        if "buy" not in described:
            print("  ! без операции buy автоматическая покупка невозможна")
            problems += 1

    # 3. Живая проверка чтения
    adapter = get_adapter(market)
    print()
    try:
        rows = await adapter.search(limit=3)
        print(f"✓ Поиск работает: получено лотов {len(rows)}")
        for row in rows[:3]:
            print(f"    {row.gift.collection} #{row.gift.number or '?'} "
                  f"— {row.price} {row.currency.value} (id={row.external_id})")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ Поиск не работает: {type(exc).__name__}: {exc}")
        problems += 1

    if adapter.supports(Capability.INVENTORY):
        try:
            owned = await adapter.inventory()
            print(f"✓ Инвентарь читается: {len(owned)} подарков "
                  f"(нужен для сверки после покупки)")
        except Exception as exc:  # noqa: BLE001
            print(f"✗ Инвентарь не читается: {type(exc).__name__}: {exc}")
            print("  Без инвентаря нельзя свести неизвестный исход покупки.")
            problems += 1
    else:
        print("✗ Инвентарь недоступен — сверка после покупки работать не будет")
        problems += 1

    # 4. Лимиты
    print()
    cap = cfg.market_trade_cap(market, adapter.native_currency)
    if cap is None:
        print(f"✗ Не задан лимит сделки "
              f"({market.value.upper()}_MAX_TRADE_TON)")
        problems += 1
    else:
        print(f"✓ Лимит одной сделки: {cap} {adapter.native_currency.value}")

    auto_on = market.value in cfg.auto_markets and cfg.allow_experimental_auto
    print(f"  Автономный режим: {'разрешён' if auto_on else 'выключен'}")

    # 5. Итог
    print()
    buy_status = adapter.status_of(Capability.BUY)
    if problems == 0 and buy_status is not CapabilityStatus.UNAVAILABLE:
        print("✓ Площадка готова к торговле с подтверждением (SEMI).")
        print("  Начните с малого лимита и проверьте первую сделку вручную.")
        return 0
    print(f"✗ К торговле не готова: проблем {problems}")
    return 1


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
    from app.services import secrets

    if not secrets.resolve("BOT_TOKEN", settings.bot_token):
        problems.append(
            "токен бота не задан — получите у @BotFather и впишите "
            "в панели (Настройки) либо в .env"
        )
    if not secrets.resolve("OWNER_IDS", settings.owner_ids).strip():
        problems.append(
            "владельцы не заданы — бот не ответит никому "
            "(узнайте свой id командой /id в боте)"
        )
    from app.adapters.telegram_gateway import gateway

    api_id, api_hash = gateway.credentials()
    if not api_id or not api_hash:
        problems.append(
            "api_id / api_hash не заданы — поиск подарков работать не будет "
            "(веб-панель → Настройки, либо https://my.telegram.org)"
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

    print(f"\nПроблем: {len(problems)}\n")
    # Веб-панели ключи Telegram не нужны — её можно поднять сразу
    # и заполнить всё остальное через браузер.
    if settings.web_password:
        print("Панель запускается без ключей Telegram — заполните их в ней:")
        print("    systemctl enable --now gift-web")
        host = settings.public_url or "http://<ip-сервера>:8081"
        print(f"    {host}/settings")
    else:
        print("Задайте WEB_PASSWORD в .env, чтобы заполнить ключи через панель.")
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
            "contract",
        ],
    )
    parser.add_argument(
        "market",
        nargs="?",
        help="площадка для команды contract (portals, mrkt, telegram, …)",
    )
    parser.add_argument(
        "--template",
        action="store_true",
        help="создать заготовку контракта вместо проверки",
    )
    args = parser.parse_args(argv)

    if args.command == "contract":
        if not args.market:
            parser.error("укажите площадку: gift-cli contract portals")
        return asyncio.run(_contract(args.market, args.template))

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
