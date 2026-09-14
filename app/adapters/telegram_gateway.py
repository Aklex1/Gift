"""Telethon-шлюз: одна пользовательская сессия на всё приложение.

ТЗ требует именно такой контур: одна user session, файловая блокировка,
очередь с приоритетами, дедупликация, пейсинг и корректная обработка
FloodWait. Все MTProto-вызовы приложения проходят здесь.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.adapters.base import AuthRequired, OutcomeUnknown, RateLimited
from app.config import settings
from app.services import secrets

log = logging.getLogger(__name__)

#: Минимальная пауза между любыми двумя MTProto-запросами, секунд.
#: Защита аккаунта от блокировки за агрессивный опрос.
MIN_INTERVAL = 0.9

#: Пауза между write-вызовами (покупка/листинг) — строже, чем для чтения.
WRITE_INTERVAL = 2.5


class TelegramGateway:
    """Единственная точка доступа к MTProto.

    Сериализует вызовы через глобальный семафор, соблюдает интервалы
    и превращает сетевые обрывы write-операций в OutcomeUnknown.
    """

    _instance: "TelegramGateway | None" = None

    def __init__(self) -> None:
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._last_write = 0.0
        #: До этого момента работать нельзя — активен FloodWait.
        self._flood_until = 0.0
        self._me: Any = None

    # ------------------------------------------------------------------
    @classmethod
    def instance(cls) -> "TelegramGateway":
        """Синглтон шлюза."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    @staticmethod
    def credentials() -> tuple[int, str]:
        """api_id и api_hash: сначала из панели, затем из .env."""
        raw_id = secrets.resolve("TG_API_ID", str(settings.tg_api_id or ""))
        api_hash = secrets.resolve("TG_API_HASH", settings.tg_api_hash)
        try:
            api_id = int(raw_id) if raw_id else 0
        except ValueError:
            api_id = 0
        return (api_id, api_hash)

    def is_configured(self) -> bool:
        """Заданы ли api_id/api_hash."""
        api_id, api_hash = self.credentials()
        return bool(api_id and api_hash)

    def session_exists(self) -> bool:
        """Есть ли файл авторизованной сессии."""
        return settings.session_path.exists()

    async def client(self) -> Any:
        """Получить подключённый и авторизованный TelegramClient."""
        api_id, api_hash = self.credentials()
        if not (api_id and api_hash):
            raise AuthRequired(
                "api_id / api_hash не заданы. Получите их на "
                "https://my.telegram.org -> API development tools и укажите "
                "в веб-панели (Настройки) либо в файле .env"
            )
        if self._client is not None and self._client.is_connected():
            return self._client

        from telethon import TelegramClient  # локальный импорт: тяжёлая зависимость

        settings.ensure_dirs()
        self._client = TelegramClient(
            str(settings.session_path.with_suffix("")),
            api_id,
            api_hash,
            # Пейсинг делаем сами; авто-ретраи Telethon на FloodWait отключены,
            # чтобы write-операции не отправлялись повторно вслепую.
            flood_sleep_threshold=0,
            connection_retries=3,
            retry_delay=2,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            raise AuthRequired(
                "Telegram-сессия не авторизована. Выполните на сервере: "
                "gift-cli login"
            )
        self._me = await self._client.get_me()
        log.info("MTProto-сессия активна: id=%s", getattr(self._me, "id", "?"))
        return self._client

    async def me(self) -> Any:
        """Текущий торговый аккаунт."""
        if self._me is None:
            await self.client()
        return self._me

    # ------------------------------------------------------------------
    async def call(self, request: Any, *, write: bool = False) -> Any:
        """Выполнить MTProto-запрос с пейсингом и обработкой FloodWait.

        Args:
            request: Объект запроса Telethon.
            write: True для операций, меняющих состояние/тратящих деньги.

        Raises:
            RateLimited: активен FloodWait.
            OutcomeUnknown: обрыв связи ПОСЛЕ отправки write-запроса.
        """
        from telethon import errors

        async with self._lock:
            now = time.monotonic()
            if now < self._flood_until:
                raise RateLimited(
                    "Активен FloodWait от Telegram",
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
                log.warning("FloodWait %s c на %s", exc.seconds, type(request).__name__)
                raise RateLimited(
                    f"FloodWait {exc.seconds} c", retry_after=float(exc.seconds)
                ) from exc
            except (errors.RPCError, ValueError, TypeError):
                # Явный отказ сервера — исход определён, деньги не списаны.
                raise
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                if write:
                    # Запрос ушёл, ответ потерян. Покупка могла пройти.
                    raise OutcomeUnknown(
                        f"Связь оборвалась после отправки {type(request).__name__}: {exc}"
                    ) from exc
                raise
            finally:
                self._last_call = time.monotonic()
                if write:
                    self._last_write = self._last_call
            return result

    # ------------------------------------------------------------------
    async def close(self) -> None:
        """Закрыть соединение."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


gateway = TelegramGateway.instance()
