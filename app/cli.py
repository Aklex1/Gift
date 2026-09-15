"""Командная строка: установка, логин, диагностика.

Команды:
    gen-key     — сгенерировать ключ шифрования секретов
    init        — создать схему БД и стартовые данные
    login       — вход в торговый аккаунт Telegram
                  (`login --account второй` для конкретного аккаунта)
    accounts    — список аккаунтов и их балансы
    whoami      — показать, под каким аккаунтом работает сессия
    balance     — балансы Stars/GRAM
    probe       — живая проверка доступности площадок
    scan        — разовый проход сканера
    inventory   — сверка портфеля с инвентарём
    doctor      — проверка конфигурации перед запуском
    env-sync    — дописать в .env новые настройки из .env.example,
                  не трогая уже заданные значения
    contract    — работа с боевым контрактом площадки:
                  `contract portals` показывает состояние,
                  `contract portals --template` создаёт заготовку
    rotate-key  — сменить ключ шифрования секретов, перешифровав базу
    verify-key  — проверить, что секреты читаются текущим ключом
    renew-tokens— продлить токены площадок через мини-приложения
    tokens      — показать состояние токенов площадок
    transfer-target — кому бот передаст подарок при переносе
    lots        — какие лоты площадки отдают прямо сейчас
                  (`lots portals`, `lots --collection "Lol Pop"`)
    feed        — прочитать канал находок и показать коллекции
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from app.config import settings
from app.enums import display_currency


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


def session_file_busy(path) -> bool:
    """Занят ли файл сессии другим процессом.

    Файл сессии Telethon — SQLite, и писать в него может только один
    процесс. Проверяем заранее: иначе Telethon падает с трассировкой
    посреди входа, уже запросив код.
    """
    import sqlite3

    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=1)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return True
    return False


def _session_busy_message(path) -> str:
    """Что именно делать, если файл сессии занят."""
    return (
        f"✗ Файл сессии занят другим процессом: {path}\n\n"
        "  Писать в него может только кто-то один. Остановите все службы\n"
        "  бота, войдите и запустите обратно:\n\n"
        "      systemctl stop gift-worker gift-bot gift-web\n"
        "      gift-cli login\n"
        "      systemctl start gift-worker gift-bot gift-web\n\n"
        "  Если службы уже остановлены, значит файл держит зависший\n"
        "  процесс — найдите его: fuser -v " + str(path)
    )


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

    # Проверяем до того, как спрашивать код: занятый файл сессии иначе
    # роняет вход трассировкой уже после ввода номера.
    if session_file_busy(path):
        print(_session_busy_message(path), file=sys.stderr)
        return 1

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
            print(f"  GRAM     : {format_amount(account.ton_balance)}")
            if state:
                print(f"  Состояние: {', '.join(state)}")
            if account.last_error:
                print(f"  Ошибка   : {account.last_error[:120]}")
            print()
    return 0


async def _whoami() -> int:
    """Показать текущий торговый аккаунт."""
    from app.adapters.telegram_gateway import default_gateway

    tg = default_gateway()
    me = await tg.me()
    print(
        f"Аккаунт {tg.label}: {getattr(me, 'first_name', '')} "
        f"(@{getattr(me, 'username', '—')}, id={getattr(me, 'id', '?')})"
    )
    await tg.close()
    return 0


async def _balance() -> int:
    """Показать балансы с сырыми значениями от Telegram.

    Сырые числа нужны, чтобы отличить настоящий ноль от ошибки
    пересчёта: Stars приходят целыми с нанодолями, GRAM — в нанотонах.
    """
    from telethon.tl import functions, types

    from app.adapters.registry import get_adapter
    from app.adapters.ton import TonClient
    from app.enums import Market
    from app.services import secrets

    adapter = get_adapter(Market.TELEGRAM)
    print(f"Аккаунт: {adapter.label}\n")

    print("--- Stars (payments.getStarsStatus) ---")
    try:
        res = await adapter.gateway.call(
            functions.payments.GetStarsStatusRequest(peer=types.InputPeerSelf())
        )
        raw = getattr(res, "balance", None)
        print(f"  сырой ответ : {raw}")
        for item in await adapter.balance():
            print(f"  {item.currency.value}: {item.amount}")
    except Exception as exc:  # noqa: BLE001
        print(f"  недоступно: {type(exc).__name__}: {exc}")

    print("\n--- GRAM внутри Telegram (payments.getStarsStatus ton=True) ---")
    try:
        res = await adapter.gateway.call(
            functions.payments.GetStarsStatusRequest(
                peer=types.InputPeerSelf(), ton=True
            )
        )
        raw = getattr(res, "balance", None)
        amount = getattr(raw, "amount", None)
        print(f"  сырой ответ : {raw}")
        if amount is not None:
            print(f"  нанотоны    : {amount}")
            print(f"  в GRAM      : {Decimal(amount) / Decimal(10) ** 9}")
    except Exception as exc:  # noqa: BLE001
        print(f"  недоступно: {type(exc).__name__}: {exc}")

    address = secrets.resolve("TON_WALLET_ADDRESS", settings.ton_wallet_address)
    print("\n--- Внешний кошелёк GRAM ---")
    if not address:
        print("  адрес не задан (панель → Настройки → Адрес кошелька GRAM)")
    else:
        ton = TonClient()
        try:
            print(f"  {address}")
            print(f"  баланс: {await ton.balance(address)} GRAM")
        except Exception as exc:  # noqa: BLE001
            print(f"  недоступно: {type(exc).__name__}: {exc}")
        finally:
            await ton.close()

    print(
        "\nЕсли здесь нули, а деньги вы видите в Telegram — проверьте, где именно:\n"
        "  • Stars          — Настройки → Мой профиль → Звёзды\n"
        "  • GRAM за подарки — приходит сюда же, в getStarsStatus(ton=True)\n"
        "  • @wallet        — ОТДЕЛЬНЫЙ сервис, боту не виден.\n"
        "                     Рубли и GRAM в @wallet сюда не попадают."
    )
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
        print(
            f"✓ Лимит одной сделки: {cap} "
            f"{display_currency(adapter.native_currency)}"
        )

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


#: Значения, которые перестали работать и подлежат замене.
#: Правится только то, что заведомо сломано — например, домен
#: площадки, который больше не существует. Ключи и секреты не
#: трогаются никогда.
OBSOLETE_VALUES: dict[str, tuple[str, str, str]] = {
    "PORTALS_BASE_URL": (
        "https://portals-market.com/api",
        "https://portals.tg/api",
        "домен portals-market.com больше не резолвится",
    ),
    "MRKT_BASE_URL": (
        "https://api.mrkt.land",
        "https://api.tgmrkt.io/api/v1",
        "прежний адрес MRKT был указан неверно",
    ),
}


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

    # Замена заведомо нерабочих значений: изменившийся домен площадки
    # иначе навсегда остался бы в файле, ведь существующие значения
    # мы принципиально не трогаем.
    replaced: list[str] = []
    for key, (old, new, why) in OBSOLETE_VALUES.items():
        needle = f"{key}={old}"
        if needle in current_text:
            current_text = current_text.replace(needle, f"{key}={new}")
            replaced.append(f"{key}: {old} -> {new} ({why})")
    if replaced:
        env_path.write_text(current_text, encoding="utf-8")

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

    for line in replaced:
        print(f"✓ Заменено {line}")

    if not added_keys:
        if not replaced:
            print("✓ Все настройки из шаблона уже есть в .env")
        else:
            print("\nПерезапустите сервисы: systemctl restart gift-bot gift-worker gift-web")
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
    from app.adapters.telegram_gateway import default_gateway

    tg = default_gateway()
    if not tg.is_configured():
        problems.append(
            "api_id / api_hash не заданы — поиск подарков работать не будет "
            "(веб-панель → Настройки, либо https://my.telegram.org)"
        )
    elif not tg.session_exists():
        problems.append(
            f"Сессия не создана: {tg.session_path} "
            f"(выполните: gift-cli login --account {tg.label})"
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

    # Секреты должны читаться текущим ключом. Иначе бот стартует,
    # но площадки молча отвечают 401 — и причина неочевидна.
    if settings.secret_key:
        try:
            from app.services import keyrotate

            readable = keyrotate.verify(settings.secret_key)
            if readable["failed"]:
                problems.append(
                    f"GIFT_SECRET_KEY не расшифровывает {len(readable['failed'])} "
                    "секрет(ов): " + ", ".join(readable["failed"][:3])
                    + ". Верните прежний ключ и смените его через gift-cli rotate-key"
                )
            elif readable["ok"]:
                print(f"✓ Секреты читаются ключом: {readable['ok']} шт.")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"не удалось проверить секреты: {exc}")

    # Свежесть резервной копии.
    backups = settings.data_dir / "backups"
    if not backups.exists():
        warnings.append(
            "резервных копий нет. Включите таймер: "
            "systemctl enable --now gift-backup.timer"
        )
    else:
        made = sorted(d for d in backups.iterdir() if d.is_dir())
        if not made:
            warnings.append("каталог бэкапов пуст — снимите копию: deploy/backup.sh")
        else:
            import datetime as _dt

            age = _dt.datetime.now() - _dt.datetime.fromtimestamp(
                made[-1].stat().st_mtime
            )
            days = age.days
            if days >= 3:
                warnings.append(
                    f"последняя резервная копия сделана {days} дн. назад "
                    f"({made[-1].name}) — проверьте gift-backup.timer"
                )
            else:
                print(f"✓ Последняя резервная копия: {made[-1].name}")

    for key, (old, _new, why) in OBSOLETE_VALUES.items():
        actual = {
            "PORTALS_BASE_URL": settings.portals_base_url,
            "MRKT_BASE_URL": settings.mrkt_base_url,
        }.get(key, "")
        if actual.rstrip("/") == old.rstrip("/"):
            problems.append(
                f"{key} указывает на нерабочий адрес: {why}. "
                f"Исправьте командой: gift-cli env-sync"
            )

    # Бюджет меньше самого дешёвого найденного лота — стратегия
    # работает вхолостую: кандидаты находятся, купить не на что.
    try:
        from app.db import session_scope
        from app.models import Budget, Candidate, Strategy

        with session_scope() as session:
            for item in session.query(Strategy).filter_by(is_enabled=True).all():
                budget = (
                    session.get(Budget, item.budget_id) if item.budget_id else None
                )
                cap = Decimal(budget.hard_cap) if budget else Decimal(0)
                cheapest = (
                    session.query(Candidate.price_stars)
                    .filter_by(strategy_id=item.id, state="pending")
                    .order_by(Candidate.price_stars.asc())
                    .limit(1)
                    .scalar()
                )
                if cheapest is None or cap <= 0:
                    continue
                if cap < Decimal(cheapest):
                    warnings.append(
                        f"стратегия {item.name}: бюджет {cap:.0f} Stars меньше "
                        f"самого дешёвого найденного лота ({Decimal(cheapest):.0f} "
                        f"Stars) — покупать не на что"
                    )
    except Exception:  # noqa: BLE001 - диагностика не должна ронять doctor
        pass

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


async def _transfer_target() -> int:
    """Показать, кому именно бот передаст подарок.

    Перенос необратим: подарок, ушедший не туда, не возвращается ничем.
    Адрес депозита Portals нигде не публикует — его показывают только в
    мини-приложении, — поэтому бот его не угадывает. Зато он может
    разрешить введённое вами имя и показать, кто за ним стоит: этого
    достаточно, чтобы поймать подделку до отправки.
    """
    from app.adapters import telegram_gateway
    from app.db import init_db
    from app.services import runtime, secrets

    init_db()
    target = (secrets.resolve("PORTALS_DEPOSIT", "") or "").strip()

    print(f"Перенос подарков: {'ВКЛЮЧЁН' if runtime.transfer_enabled() else 'выключен'}")
    if not target:
        print(
            "\n✗ Получатель не задан.\n\n"
            "  Где взять: мини-приложение Portals → «Пополнить» →\n"
            "  раздел про подарки (не про GRAM). Там указан аккаунт,\n"
            "  которому нужно передать подарок.\n\n"
            "  Вписать: панель → «Настройки» → «Куда переносить подарки\n"
            "  для Portals».",
            file=sys.stderr,
        )
        return 1

    print(f"Задан получатель: {target}\n")

    # Самая вероятная и самая дорогая ошибка: сюда вписывают адрес
    # кошелька, который Portals показывает для пополнения. Он для
    # денег — сам Portals предупреждает «только GRAM и токены TON».
    # Подарок — это NFT, и передаётся он аккаунту Telegram, а не на
    # адрес в блокчейне.
    if secrets.looks_like_ton_address(target):
        print(
            "✗ Это адрес кошелька TON, а не аккаунт Telegram.\n\n"
            "  Portals показывает такой адрес для пополнения БАЛАНСА и сам\n"
            "  предупреждает: на него отправляют только GRAM и токены TON.\n"
            "  Подарок туда отправлять нельзя — он не вернётся.\n\n"
            "  Подарки передаются аккаунту Telegram. Ищите это в\n"
            "  мини-приложении Portals в разделе «Гифты», а не на\n"
            "  странице кошелька.",
            file=sys.stderr,
        )
        return 1

    tg = telegram_gateway.default_gateway()
    try:
        client = await tg.client()
        entity = await client.get_entity(target)
    except Exception as exc:  # noqa: BLE001 - показываем причину, не падаем
        print(f"✗ Не удалось найти {target}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("  Проверьте написание. Несуществующий получатель — это "
              "потерянный подарок.", file=sys.stderr)
        return 1

    name = " ".join(
        x for x in (getattr(entity, "first_name", ""), getattr(entity, "last_name", ""))
        if x
    ) or getattr(entity, "title", "") or "—"

    print("Telegram отвечает, что это:")
    print(f"  имя       : {name}")
    print(f"  username  : @{getattr(entity, 'username', None) or '—'}")
    print(f"  id        : {getattr(entity, 'id', '—')}")
    print(f"  бот       : {'да' if getattr(entity, 'bot', False) else 'нет'}")
    print(f"  проверен  : {'да' if getattr(entity, 'verified', False) else 'нет'}")

    danger = []
    if getattr(entity, "scam", False):
        danger.append("Telegram пометил аккаунт как МОШЕННИЧЕСКИЙ")
    if getattr(entity, "fake", False):
        danger.append("Telegram пометил аккаунт как ПОДДЕЛЬНЫЙ")
    if getattr(entity, "restricted", False):
        danger.append("аккаунт ограничен Telegram")

    if danger:
        print("\n🚨 ОПАСНО:")
        for item in danger:
            print(f"  • {item}")
        print("  Ни в коем случае не включайте перенос на этот аккаунт.")
        return 1

    print(
        "\nСверьте это с тем, что показывает мини-приложение Portals.\n"
        "Совпадает — можно включать перенос на странице «Торговля».\n"
        "Не совпадает — исправьте настройку: ошибка здесь стоит подарка."
    )
    await tg.close()
    return 0


async def _tokens() -> int:
    """Показать, что лежит в токенах площадок, ничего не меняя."""
    from app.services import webauth

    print("Токены площадок\n")
    for market in webauth.MINIAPPS:
        state = webauth.token_state(market)
        bot, short_name = webauth._miniapp(market)
        print(f"  {state['market']}")
        print(f"    значение    : {state['masked']}")
        print(f"    состояние   : {state['note']}")
        print(f"    мини-приложение: @{bot}/{short_name}")
        if state["present"] and state["age_min"] is None:
            print("    Токен задан вручную и продолжает работать до своего")
            print("    истечения, даже если автопродление не удаётся.")
        print()

    print("Продлить: gift-cli renew-tokens")
    return 0


async def _renew_tokens() -> int:
    """Продлить токены площадок прямо сейчас."""
    from app.services import webauth

    reports = await webauth.renew_all(force=True)
    failed = 0
    for report in reports:
        if report["ok"]:
            print(f"✓ {report['market']}: {report['detail']}")
        else:
            failed += 1
            print(f"✗ {report['market']}: {report['detail']}")

    if failed:
        print(
            "\nПродление идёт через мини-приложение от имени торгового "
            "аккаунта.\nЕсли не работает — проверьте вход (gift-cli whoami) "
            "и адрес\nмини-приложения (настройки PORTALS_MINIAPP / MRKT_MINIAPP)."
        )
    return 1 if failed == len(reports) else 0


async def _lots(market_name: str | None = None, collection: str | None = None) -> int:
    """Показать, какие лоты площадки отдают прямо сейчас.

    Отвечает на вопрос, который не решают ни probe, ни scan: probe
    говорит лишь «площадка жива», а scan уже отфильтрован стратегиями.
    Здесь — сырой поиск без фильтров, чтобы отличить неработающую
    площадку от работающей, но с неподходящими коллекциями.
    """
    from app.adapters.base import Capability
    from app.adapters.registry import get_adapter
    from app.db import init_db, session_scope
    from app.enums import Currency, Market
    from app.services import marketdata

    init_db()

    if market_name:
        try:
            markets = [Market(market_name.lower())]
        except ValueError:
            print(f"✗ Неизвестная площадка: {market_name}", file=sys.stderr)
            return 1
    else:
        markets = [Market.TELEGRAM, Market.PORTALS, Market.MRKT]

    problems = 0
    for market in markets:
        print(f"\n=== {market.value} ===")
        adapter = get_adapter(market)

        if not adapter.supports(Capability.SEARCH):
            reason = (
                "сессия не авторизована (gift-cli login)"
                if market is Market.TELEGRAM
                else "нет токена площадки — задайте в «Настройках»"
            )
            print(f"✗ Поиск недоступен: {reason}")
            problems += 1
            continue

        try:
            rows = await adapter.search(collection=collection, limit=20)
        except Exception as exc:  # noqa: BLE001 - показываем причину, не падаем
            print(f"✗ Поиск не удался: {type(exc).__name__}: {exc}")
            problems += 1
            continue

        where = f"по коллекции {collection!r}" if collection else "без фильтра"
        print(f"Лотов получено ({where}): {len(rows)}")
        if not rows:
            print("  Площадка ответила, но предложений нет.")
            print("  Это не поломка: попробуйте другую коллекцию или без фильтра.")
            continue

        with session_scope() as session:
            print(f"\n  {'подарок':<34} {'цена':>16} {'≈ Stars':>10}")
            for row in sorted(rows, key=lambda r: r.price)[:10]:
                gift = row.gift
                name = f"{gift.collection or '?'} #{gift.number or '?'}"
                if gift.model:
                    name += f" · {gift.model}"
                in_stars = marketdata.to_stars(session, row.price, row.currency)
                price = f"{row.price} {display_currency(row.currency)}"
                stars = f"{in_stars:,.0f}".replace(",", " ") if in_stars else "—"
                print(f"  {name[:34]:<34} {price:>16} {stars:>10}")

    # Названия коллекций из стратегий — самая частая причина пустого скана.
    await _check_strategy_collections()

    if problems:
        print(f"\nПлощадок с проблемой: {problems}")
    return 1 if problems == len(markets) else 0


async def _check_strategy_collections() -> None:
    """Сверить коллекции включённых стратегий с каталогом Telegram."""
    from app.adapters.registry import get_adapter
    from app.adapters.telegram_mtproto import TelegramAdapter
    from app.db import session_scope
    from app.enums import Market
    from app.services import strategy as strategy_service

    with session_scope() as session:
        wanted: dict[str, list[str]] = {
            s.name: list(s.collections or [])
            for s in strategy_service.active_strategies(session)
        }
    if not wanted:
        print("\n=== стратегии ===\nВключённых стратегий нет — сканер ничего не ищет.")
        return

    adapter = get_adapter(Market.TELEGRAM)
    if not isinstance(adapter, TelegramAdapter):
        return
    try:
        catalog = await adapter.catalog()
    except Exception as exc:  # noqa: BLE001 - без каталога просто молчим
        print(f"\n=== стратегии ===\nКаталог Telegram недоступен: {exc}")
        return

    print(f"\n=== коллекции стратегий ===")
    print(f"В каталоге Telegram всего коллекций: {len(catalog)}")
    for name, collections in wanted.items():
        if not collections:
            print(f"  {name}: коллекции не заданы — ищется весь рынок")
            continue
        missing = [c for c in collections if c.strip().lower() not in catalog]
        found = len(collections) - len(missing)
        print(f"  {name}: задано {len(collections)}, найдено в каталоге {found}")
        if missing:
            print(f"    ✗ нет в каталоге: {', '.join(missing)}")
            print(f"    Такие коллекции Telegram не отдаёт — поиск по ним "
                  f"возвращает ноль.")


async def _feed() -> int:
    """Прочитать канал находок и показать, что из него вышло."""
    from app.db import session_scope
    from app.services import feed
    from app.services import strategy as strategy_service

    if not feed.channel_ref():
        print("✗ Канал не задан. Укажите его в панели → Настройки → "
              "«Канал находок»", file=sys.stderr)
        return 1

    print(f"Канал: {feed.channel_ref()}")
    report = await feed.sync()
    if report.get("error"):
        print(f"✗ {report['error']}", file=sys.stderr)
        return 1

    print(
        f"Постов с находками: {report['posts']}, "
        f"находок: {report['finds']}, новых: {report['added']}\n"
    )

    with session_scope() as session:
        scores = feed.rank_collections(session)
        if not scores:
            print("Находок за последние две недели нет.")
            return 0

        print(f"{'Коллекция':<24} {'всего':>6} {'продано':>8} {'вес':>7} {'медиана':>9}")
        for item in scores:
            print(
                f"{item.collection:<24} {item.finds:>6} {item.realized:>8} "
                f"{item.score:>7.2f} {item.median_price:>9.2f}"
            )

        target = strategy_service.feed_strategy(session)
        if target is None:
            print("\nСтратегия канала не заведена — включите её в панели "
                  "на странице «Канал находок».")
            return 0

        result = strategy_service.refresh_feed_collections(session)
        state = "включена" if target.is_enabled else "выключена"
        print(f"\nСтратегия канала: {state}")
        if result.get("ok"):
            print(f"  коллекции: {', '.join(result['collections'])}")
            print(f"  потолок цены: {result.get('max_price_stars') or '—'} Stars")

    print(
        "\nПомните: пост показывает лучшие покупки из сотен, а «Оценка» —\n"
        "это их прикидка, не сделка. Доверять стоит столбцу «продано»."
    )
    return 0


def cmd_verify_key() -> int:
    """Проверить, что все секреты в базе читаются текущим ключом."""
    from app.services import keyrotate

    if not (settings.secret_key or "").strip():
        print("✗ GIFT_SECRET_KEY не задан", file=sys.stderr)
        return 1

    result = keyrotate.verify(settings.secret_key)
    if result["failed"]:
        print(f"✗ Не читаются текущим ключом ({len(result['failed'])}):")
        for item in result["failed"]:
            print(f"    • {item}")
        print(f"  Читаются: {result['ok']}")
        print("  Похоже, GIFT_SECRET_KEY заменён без перешифрования базы.")
        print("  Верните прежний ключ и смените его через: gift-cli rotate-key")
        return 1

    if result["ok"] == 0:
        print("• Зашифрованных секретов в базе нет — проверять нечего")
        return 0
    print(f"✓ Все секреты читаются текущим ключом ({result['ok']} шт.)")
    return 0


def cmd_rotate_key(new_key: str | None, dry_run: bool) -> int:
    """Сменить ключ шифрования: перешифровать базу и обновить .env."""
    from app.config import BASE_DIR
    from app.services import keyrotate

    env_path = BASE_DIR / ".env"
    old_key = keyrotate.read_env_key(env_path) or settings.secret_key
    if not (old_key or "").strip():
        print("✗ Текущий GIFT_SECRET_KEY не найден ни в .env, ни в окружении",
              file=sys.stderr)
        return 1

    # Старый ключ обязан подходить ко всем данным: иначе смена ключа
    # тихо превратится в потерю части секретов.
    check = keyrotate.verify(old_key)
    if check["failed"]:
        print("✗ Текущий ключ не читает часть секретов — сначала почините это:",
              file=sys.stderr)
        for item in check["failed"]:
            print(f"    • {item}", file=sys.stderr)
        return 1

    target = (new_key or "").strip() or keyrotate.generate_key()
    overview = keyrotate.plan(target)

    print("Будет перешифровано:")
    print(f"  настроек: {len(overview['settings'])}", end="")
    if overview["settings"]:
        print(f"  ({', '.join(overview['settings'])})", end="")
    print()
    print(f"  аккаунтов: {len(overview['accounts'])}", end="")
    if overview["accounts"]:
        print(f"  ({', '.join(overview['accounts'])})", end="")
    print()

    if dry_run:
        print("\n• Пробный прогон: ничего не изменено.")
        print("  Для реальной смены повторите без --dry-run.")
        return 0

    try:
        report = keyrotate.rotate(old_key, target, actor="cli")
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    if keyrotate.write_env_key(env_path, target):
        print(f"✓ Новый ключ записан в {env_path}")
    else:
        print(f"⚠ Не удалось обновить {env_path} — впишите строку вручную:")
        print(f"    {keyrotate.ENV_KEY}={target}")

    print(
        f"✓ Перешифровано: настроек {report['settings']}, "
        f"аккаунтов {report['accounts']}"
    )
    print("\nДальше обязательно:")
    print("  1) сохраните новый ключ вне сервера — без него база бесполезна;")
    print("  2) перезапустите сервисы, иначе они работают со старым ключом:")
    print("     systemctl restart gift-web gift-bot gift-worker")
    return 0


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
            "rotate-key",
            "verify-key",
            "renew-tokens",
            "tokens",
            "transfer-target",
            "lots",
            "feed",
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
    parser.add_argument(
        "--new",
        dest="new_key",
        help="новый ключ для rotate-key (по умолчанию генерируется)",
    )
    parser.add_argument(
        "--collection",
        help="коллекция для команды lots",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="для rotate-key: показать план, ничего не меняя",
    )
    args = parser.parse_args(argv)

    # CLI запускается рядом с работающим воркером, который держит файл
    # сессии. Работаем с копией ключа — кроме login, который эту сессию
    # и создаёт, а значит обязан писать в файл.
    if args.command != "login":
        from app.adapters import telegram_gateway

        telegram_gateway.prefer_detached()

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

    if args.command == "lots":
        return asyncio.run(_lots(args.market, args.collection))

    if args.command == "rotate-key":
        return cmd_rotate_key(args.new_key, args.dry_run)
    if args.command == "verify-key":
        return cmd_verify_key()

    if args.command == "login":
        return asyncio.run(_login(args.account))

    async_commands = {
        "whoami": _whoami,
        "accounts": _accounts,
        "balance": _balance,
        "probe": _probe,
        "scan": _scan,
        "inventory": _inventory,
        "renew-tokens": _renew_tokens,
        "tokens": _tokens,
        "transfer-target": _transfer_target,
        "feed": _feed,
    }
    return asyncio.run(async_commands[args.command]())


if __name__ == "__main__":
    raise SystemExit(main())
