"""Telethon-шлюзы: по одной сессии на торговый аккаунт.

ТЗ требует аккуратного обращения с пользовательской сессией: очередь,
пейсинг, дедупликация и корректная обработка FloodWait. Всё это
сохраняется, но теперь на каждый аккаунт свой экземпляр: FloodWait
Telegram считается по аккаунту, и блокировка одного не должна
останавливать остальные.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from app.adapters.base import AuthRequired, OutcomeUnknown, RateLimited
from app.config import settings

log = logging.getLogger(__name__)

#: Минимальная пауза между любыми двумя запросами одного аккаунта.
MIN_INTERVAL = 0.9

#: Пауза между write-вызовами — строже, чем для чтения.
WRITE_INTERVAL = 2.5


class TelegramGateway:
    """Единственная точка доступа к MTProto для одного аккаунта."""

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_path: Path,
        label: str = "основной",
        account_id: int | None = None,
        detached: bool = False,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.label = label
        self.account_id = account_id
        #: Работать с копией сессии в памяти, не трогая файл.
        #: Файл сессии — это SQLite, и Telethon пишет в него при каждом
        #: обновлении кэша сущностей. Два процесса на одном файле дают
        #: «database is locked», поэтому короткоживущим потребителям
        #: (веб-панель) достаётся копия, а файл остаётся за воркером.
        self.detached = detached

        self._client: Any = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._last_write = 0.0
        #: До этого момента работать нельзя — активен FloodWait.
        self._flood_until = 0.0
        self._me: Any = None

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """Заданы ли учётные данные."""
        return bool(self.api_id and self.api_hash)

    def session_exists(self) -> bool:
        """Есть ли файл авторизованной сессии."""
        return self.session_path.exists()

    @property
    def flood_seconds_left(self) -> float:
        """Сколько секунд осталось до конца FloodWait."""
        return max(0.0, self._flood_until - time.monotonic())

    def _session_arg(self) -> Any:
        """Что передать Telethon: путь к файлу или копию в памяти.

        Обычный режим — путь: сессия живёт между перезапусками.
        Detached — копия в памяти: ключ авторизации тот же, но запись
        в общий файл не идёт, и второй процесс не упирается в
        заблокированный SQLite.
        """
        if not self.detached:
            return str(self.session_path.with_suffix(""))

        from telethon.sessions import MemorySession, SQLiteSession

        memory = MemorySession()
        if not self.session_path.exists():
            return memory

        # Читаем файл и сразу закрываем: держать его открытым — ровно
        # то, чего мы здесь избегаем.
        source = SQLiteSession(str(self.session_path.with_suffix("")))
        try:
            memory.set_dc(source.dc_id, source.server_address, source.port)
            memory.auth_key = source.auth_key
        finally:
            source.close()
        return memory

    async def client(self) -> Any:
        """Получить подключённый и авторизованный TelegramClient."""
        if not self.is_configured():
            raise AuthRequired(
                f"Аккаунт {self.label}: не заданы api_id / api_hash. "
                "Укажите их в панели, раздел «Аккаунты»."
            )
        if self._client is not None and self._client.is_connected():
            return self._client

        from telethon import TelegramClient  # локальный импорт: тяжёлая зависимость

        settings.ensure_dirs()
        self._client = TelegramClient(
            self._session_arg(),
            self.api_id,
            self.api_hash,
            # Пейсинг делаем сами; авто-ретраи Telethon на FloodWait
            # отключены, чтобы write-операции не уходили повторно вслепую.
            flood_sleep_threshold=0,
            connection_retries=3,
            retry_delay=2,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise AuthRequired(
                f"Аккаунт {self.label}: сессия не авторизована. "
                f"Выполните на сервере: gift-cli login --account {self.label}"
            )
        self._me = await self._client.get_me()
        log.info(
            "Аккаунт %s: сессия активна (id=%s)",
            self.label,
            getattr(self._me, "id", "?"),
        )
        return self._client

    async def me(self) -> Any:
        """Текущий торговый аккаунт."""
        if self._me is None:
            await self.client()
        return self._me

    # ------------------------------------------------------------------
    async def call(self, request: Any, *, write: bool = False) -> Any:
        """Выполнить MTProto-запрос с пейсингом и обработкой FloodWait.

        Raises:
            RateLimited: активен FloodWait.
            OutcomeUnknown: обрыв связи ПОСЛЕ отправки write-запроса.
        """
        from telethon import errors

        async with self._lock:
            now = time.monotonic()
            if now < self._flood_until:
                raise RateLimited(
                    f"Аккаунт {self.label}: активен FloodWait",
                    retry_after=self._flood_until - now,
                )

            # Пейсинг: выдерживаем минимальный интервал.
            gap = MIN_INTERVAL - (now - self._last_call)
            if write:
                gap = max(gap, WRITE_INTERVAL - (now - self._last_write))
            if gap > 0:
                await asyncio.sleep(gap)

            client = await self.client()
            try:
                result = await client(request)
            except errors.FloodWaitError as exc:
                self._flood_until = time.monotonic() + exc.seconds + 1
                log.warning(
                    "Аккаунт %s: FloodWait %s c на %s",
                    self.label,
                    exc.seconds,
                    type(request).__name__,
                )
                self._remember_flood(exc.seconds)
                raise RateLimited(
                    f"Аккаунт {self.label}: FloodWait {exc.seconds} c",
                    retry_after=float(exc.seconds),
                ) from exc
            except (errors.RPCError, ValueError, TypeError):
                # Явный отказ сервера — исход определён, деньги не списаны.
                raise
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                if write:
                    # Запрос ушёл, ответ потерян. Покупка могла пройти.
                    raise OutcomeUnknown(
                        f"Аккаунт {self.label}: связь оборвалась после отправки "
                        f"{type(request).__name__}: {exc}"
                    ) from exc
                raise
            finally:
                self._last_call = time.monotonic()
                if write:
                    self._last_write = self._last_call
            return result

    def _remember_flood(self, seconds: float) -> None:
        """Записать FloodWait в карточку аккаунта, чтобы его не опрашивали."""
        if self.account_id is None:
            return
        try:
            from app.db import session_scope
            from app.services import accounts as accounts_service

            with session_scope() as session:
                accounts_service.mark_flood(session, self.account_id, seconds)
        except Exception as exc:  # noqa: BLE001 - учёт не должен ломать вызов
            log.debug("Не удалось записать FloodWait аккаунта: %s", exc)

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Закрыть соединение."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


# ----------------------------------------------------------------------
# Реестр шлюзов
# ----------------------------------------------------------------------
#: account_id -> шлюз. Ключ None — аккаунт из .env (до миграции).
_gateways: dict[int | None, TelegramGateway] = {}

#: Работать с копией сессии в памяти вместо общего файла.
#:
#: Файл сессии Telethon — это SQLite, и запись в него идёт при каждом
#: обновлении кэша сущностей. Держать его могут только из одного
#: процесса: второй получает «database is locked». Владельцем назначен
#: воркер — он работает с Telegram постоянно. Панель, бот и CLI
#: обращаются изредка и коротко, поэтому им достаётся копия ключа.
#:
#: Исключение — `gift-cli login`: он создаёт сессию и обязан писать
#: в файл.
_prefer_detached = False


def prefer_detached(value: bool = True) -> None:
    """Объявить процесс неосновным потребителем сессии.

    Вызывается на старте панели, бота и CLI. Уже созданные шлюзы
    сбрасываются: иначе процесс продолжил бы держать файл.
    """
    global _prefer_detached
    if _prefer_detached == value:
        return
    _prefer_detached = value
    _gateways.clear()
    log.debug("Режим сессии: %s", "копия в памяти" if value else "общий файл")


def gateway_for(account: Any) -> TelegramGateway:
    """Получить (и закэшировать) шлюз аккаунта."""
    from app.services import accounts as accounts_service

    key = account.id
    existing = _gateways.get(key)
    api_hash = accounts_service.api_hash_of(account)
    path = accounts_service.session_path(account)

    # Учётные данные могли поменяться в панели — пересоздаём шлюз.
    if existing is not None and (
        existing.api_id == account.api_id
        and existing.api_hash == api_hash
        and existing.session_path == path
        and existing.detached == _prefer_detached
    ):
        return existing

    gateway = TelegramGateway(
        api_id=account.api_id,
        api_hash=api_hash,
        session_path=path,
        label=account.name,
        account_id=account.id,
        detached=_prefer_detached,
    )
    _gateways[key] = gateway
    return gateway


def legacy_gateway() -> TelegramGateway:
    """Шлюз по данным из .env — для установок без таблицы аккаунтов."""
    existing = _gateways.get(None)
    if existing is not None and existing.detached == _prefer_detached:
        return existing

    from app.services import secrets

    raw_id = secrets.resolve("TG_API_ID", str(settings.tg_api_id or ""))
    try:
        api_id = int(raw_id) if raw_id else 0
    except ValueError:
        api_id = 0

    gateway = TelegramGateway(
        api_id=api_id,
        api_hash=secrets.resolve("TG_API_HASH", settings.tg_api_hash),
        session_path=settings.session_path,
        label="основной",
        detached=_prefer_detached,
    )
    _gateways[None] = gateway
    return gateway


def default_gateway() -> TelegramGateway:
    """Шлюз аккаунта по умолчанию.

    Берётся первый пригодный аккаунт из таблицы; если таблица пуста,
    используются данные из .env.
    """
    try:
        from app.db import session_scope
        from app.services import accounts as accounts_service

        with session_scope() as session:
            rows = accounts_service.all_accounts(session)
            for account in rows:
                if account.is_active:
                    return gateway_for(account)
    except Exception as exc:  # noqa: BLE001 - БД может быть недоступна
        log.debug("Список аккаунтов недоступен: %s", exc)
    return legacy_gateway()


def detached_gateway(account: Any | None = None) -> TelegramGateway:
    """Одноразовый шлюз, не трогающий общий файл сессии.

    Нужен процессам, которые обращаются к Telegram изредка и коротко —
    прежде всего веб-панели. Файл сессии остаётся за воркером, который
    держит его постоянно; панель работает с копией ключа в памяти.
    Кэш сущностей при этом не сохраняется — цена одного лишнего
    запроса к Telegram против «database is locked».

    Шлюз не кэшируется: он одноразовый и должен закрываться вызывающим.
    """
    base = default_gateway() if account is None else gateway_for(account)
    return TelegramGateway(
        api_id=base.api_id,
        api_hash=base.api_hash,
        session_path=base.session_path,
        label=base.label,
        account_id=base.account_id,
        detached=True,
    )


async def close_all() -> None:
    """Закрыть все сессии."""
    for gateway in list(_gateways.values()):
        await gateway.close()
    _gateways.clear()


def forget(account_id: int | None) -> None:
    """Забыть шлюз аккаунта — например, после смены ключей."""
    _gateways.pop(account_id, None)
