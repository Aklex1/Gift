"""Командная строка: установка, логин, диагностика.

Команды:
    gen-key     — сгенерировать ключ шифрования секретов
    init        — создать схему БД и стартовые данные
    login       — вход в торговый аккаунт Telegram
                  (`login --account второй` для конкретного аккаунта)
    accounts    — список аккаунтов и их балансы
    whoami      — показать, под каким аккаунтом работает сессия
    balance     — балансы Stars/TON
    probe       — живая проверка доступности площадок
    scan        — разовый проход сканера
    inventory   — сверка портфеля с инвентарём
    doctor      — проверка конфигурации перед запуском
    env-sync    — дописать в .env новые настройки из .env.example,
                  не трогая уже заданные значения
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
        # Прежняя сессия из .env становится первым аккаунтом, чтобы
        # обновление не потеряло уже выполненный вход.
        from app.services.accounts import adopt_legacy

        adopted = adopt_legacy(session)
    sync_capabilities()
    if adopted is not None:
        print(f"✓ Существующая сессия перенесена в аккаунт {adopted.name!r}")
    print("✓ База данных готова, стартовая стратегия создана (выключена, режим SAFE)")
    return 0


async def _login(account_name: str | None = None) -> int:
    """Интерактивный вход в торговый аккаунт.

    Без имени берётся первый аккаунт из таблицы, а если её ещё не
    заполняли — данные из .env (для совместимости с прежней установкой).
    """
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    from app.db import init_db, session_scope
    from app.models import Account
    from app.services import accounts as accounts_service

    init_db()

    account_id: int | None = None
    with session_scope() as session:
        # Прежняя сессия из .env переносится в таблицу, чтобы не
        # потерять уже работающий аккаунт.
        accounts_service.adopt_legacy(session)

        rows = accounts_service.all_accounts(session)
        if account_name:
            match = next(
                (a for a in rows if a.name.lower() == account_name.lower()), None
            )
            if match is None:
                print(f"✗ Аккаунт {account_name!r} не найден.", file=sys.stderr)
                if rows:
                    print("  Доступные: " + ", ".join(a.name for a in rows),
                          file=sys.stderr)
                else:
                    print("  Добавьте его в панели, раздел «Аккаунты».",
                          file=sys.stderr)
                return 1
            chosen = match
        elif rows:
            chosen = rows[0]
        else:
            print(
                "✗ Аккаунтов нет и api_id/api_hash в .env не заданы.\n"
                "  Добавьте аккаунт в панели: раздел «Аккаунты».",
                file=sys.stderr,
            )
            return 1

        account_id = chosen.id
        label = chosen.name
        api_id = chosen.api_id
        api_hash = accounts_service.api_hash_of(chosen)
        phone_hint = chosen.phone
        path = accounts_service.session_path(chosen)

    if not api_id or not api_hash:
        print(f"✗ Аккаунт {label}: не заданы api_id / api_hash", file=sys.stderr)
        return 1

    settings.ensure_dirs()
    print(f"Вход в аккаунт: {label}")
    client = TelegramClient(str(path.with_suffix("")), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        phone = phone_hint or input("Номер телефона (в формате +7…): ").strip()
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
    print(f"  Файл сессии: {path}")
    await client.disconnect()

    with session_scope() as session:
        accounts_service.update_identity(
            session,
            account_id,
            tg_user_id=getattr(me, "id", None),
            username=getattr(me, "username", None),
        )
    return 0


async def _accounts() -> int:
    """Показать аккаунты и их балансы."""
    from app.db import init_db, session_scope
    from app.services import accounts as accounts_service

    init_db()
    with session_scope() as session:
        accounts_service.adopt_legacy(session)
        rows = accounts_service.all_accounts(session)
        cards = [
            {
                "name": a.name,
                "api_id": a.api_id,
                "username": a.tg_username,
                "active": a.is_active,
                "trade": a.can_trade,
                "authorized": accounts_service.is_authorized(a),
                "stars": a.stars_balance,
                "ton": a.ton_balance,
                "flood": a.flood_until,
                "error": a.last_error,
            }
            for a in rows
        ]

    if not cards:
        print("Аккаунтов нет. Добавьте в панели: раздел «Аккаунты».")
        return 0

    print("Опрашиваю балансы…\n")
    await accounts_service.refresh_balances()

    with session_scope() as session:
        for account in accounts_service.all_accounts(session):
            state = []
            if not account.is_active:
                state.append("выключен")
            if not account.can_trade:
                state.append("только поиск")
            if not accounts_service.is_authorized(account):
                state.append("ВХОД НЕ ВЫПОЛНЕН")
            if account.flood_until:
                state.append(f"пауза до {account.flood_until:%H:%M}")

            print(f"{account.name}")
            print(f"  Telegram : @{account.tg_username or '—'} "
                  f"(api_id {account.api_id})")
            from app.services.gifts import format_amount, format_stars

            print(f"  Stars    : {format_stars(account.stars_balance)} ★")
            print(f"  TON      : {format_amount(account.ton_balance)}")
            if state:
                print(f"  Состояние: {', '.join(state)}")
            if account.last_error:
                print(f"  Ошибка   : {account.last_error[:120]}")
            print()
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

    # 2. Контракт.
    # Берём его у самого адаптера: там уже слиты встроенные пути
    # площадки и переопределения из файла — ровно то, чем адаптер
    # будет пользоваться при сделке.
    adapter = get_adapter(market)
    contract = getattr(adapter, "contract", None)
    described = contract.described if contract else []
    path = contract_path(market.value)

    if path.exists() and is_placeholder(market.value):
        print(f"✗ Контракт не заполнен (остались заглушки ЗАПОЛНИТЕ): {path}")
        problems += 1
    elif described:
        source = f"файл {path}" if path.exists() else "встроенные эндпоинты площадки"
        print(f"✓ Контракт: {source}")
        for op in WRITE_OPS:
            mark = "✓" if op in described else "·"
            endpoint = contract.get(op) if contract else None
            detail = f"{endpoint.method} {endpoint.path}" if endpoint else "не описана"
            print(f"    {mark} {op}: {detail}")
        if "buy" not in described:
            print("  ! без операции buy автоматическая покупка невозможна")
            problems += 1
        if not path.exists():
            print(f"    переопределить: gift-cli contract {market.value} --template")
    else:
        print(f"✗ Боевые эндпоинты неизвестны: {path} не создан")
        print(f"  Создайте заготовку: gift-cli contract {market.value} --template")
        problems += 1

    # 3. Живая проверка чтения
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


def cmd_env_sync() -> int:
    """Добавить в .env настройки, появившиеся в новых версиях.

    Установщик копирует .env.example только при первой установке, и
    после обновления в рабочем файле не хватает новых ключей. Здесь
    они дописываются вместе с поясняющими комментариями; уже заданные
    значения не трогаются.
    """
    import datetime as _dt

    from app.config import BASE_DIR

    env_path = BASE_DIR / ".env"
    example_path = BASE_DIR / ".env.example"

    if not example_path.exists():
        print(f"✗ Не найден шаблон: {example_path}", file=sys.stderr)
        return 1
    if not env_path.exists():
        env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"✓ Создан {env_path} из шаблона")
        return 0

    def keys_of(text: str) -> set[str]:
        """Имена настроек, заданных в файле."""
        out = set()
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                out.add(line.split("=", 1)[0].strip())
        return out

    current_text = env_path.read_text(encoding="utf-8")
    have = keys_of(current_text)

    # Собираем недостающие настройки вместе с комментариями над ними.
    missing: list[str] = []
    pending_comments: list[str] = []
    added_keys: list[str] = []

    for line in example_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            pending_comments = []
            continue
        if stripped.startswith("#"):
            pending_comments.append(line)
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in have:
            pending_comments = []
            continue
        if missing:
            missing.append("")
        missing.extend(pending_comments)
        missing.append(line)
        added_keys.append(key)
        pending_comments = []

    if not added_keys:
        print("✓ Все настройки из шаблона уже есть в .env")
        return 0

    stamp = _dt.datetime.now().strftime("%Y-%m-%d")
    block = (
        f"\n\n# =====================================================\n"
        f"#  Добавлено при обновлении {stamp}\n"
        f"# =====================================================\n"
        + "\n".join(missing)
        + "\n"
    )
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write(block)

    print(f"✓ В {env_path} добавлено настроек: {len(added_keys)}")
    for key in added_keys:
        print(f"    {key}")
    print("\nЗначения проставлены по умолчанию — проверьте и при необходимости")
    print("поправьте, затем: systemctl restart gift-bot gift-worker gift-web")
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
            "env-sync",
            "accounts",
        ],
    )
    parser.add_argument(
        "--account",
        help="имя аккаунта для команды login",
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

    sync_commands = {
        "gen-key": cmd_gen_key,
        "init": cmd_init,
        "doctor": cmd_doctor,
        "env-sync": cmd_env_sync,
    }
    if args.command in sync_commands:
        return sync_commands[args.command]()

    if args.command == "login":
        return asyncio.run(_login(args.account))

    async_commands = {
        "whoami": _whoami,
        "accounts": _accounts,
        "balance": _balance,
        "probe": _probe,
        "scan": _scan,
        "inventory": _inventory,
    }
    return asyncio.run(async_commands[args.command]())


if __name__ == "__main__":
    raise SystemExit(main())
