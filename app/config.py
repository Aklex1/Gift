"""Конфигурация приложения.

Единственный источник правды — переменные окружения (файл .env).
Секреты площадок хранятся в БД в зашифрованном виде, здесь — только
то, что нужно для старта процесса.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.enums import TradeMode

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Настройки, читаемые из окружения и .env."""

    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------
    # Базовое
    # ------------------------------------------------------------------
    app_name: str = "gift"
    env: str = Field(default="prod", description="prod | dev | test")
    debug: bool = False
    data_dir: Path = Field(default=Path("/var/lib/gift"))
    log_level: str = "INFO"

    #: Ключ шифрования секретов в БД (Fernet, base64, 32 байта).
    #: Генерируется установщиком: `python -m app.cli gen-key`.
    secret_key: str = Field(default="", alias="GIFT_SECRET_KEY")

    # ------------------------------------------------------------------
    # Хранилище
    # ------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://gift:gift@127.0.0.1:5432/gift",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://127.0.0.1:6379/0", alias="REDIS_URL")

    # ------------------------------------------------------------------
    # Telegram: бот-интерфейс (aiogram)
    # ------------------------------------------------------------------
    #: Токен бота от @BotFather — это ТОЛЬКО интерфейс управления.
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    #: Telegram user id владельца. Fail-closed: пустой список = бот никого не пускает.
    owner_ids: str = Field(default="", alias="OWNER_IDS")

    # ------------------------------------------------------------------
    # Telegram: торговый аккаунт (MTProto / Telethon)
    # ------------------------------------------------------------------
    #: api_id и api_hash с https://my.telegram.org -> API development tools.
    #: Именно эта пара даёт доступ к resale-маркету подарков.
    tg_api_id: int = Field(default=0, alias="TG_API_ID")
    tg_api_hash: str = Field(default="", alias="TG_API_HASH")
    #: Имя файла Telethon-сессии внутри data_dir.
    tg_session_name: str = Field(default="trading", alias="TG_SESSION_NAME")
    #: Номер телефона торгового аккаунта (для интерактивного логина через CLI).
    tg_phone: str = Field(default="", alias="TG_PHONE")

    # ------------------------------------------------------------------
    # Внешние площадки (приватные API — статус experimental)
    # ------------------------------------------------------------------
    portals_base_url: str = Field(
        default="https://portals-market.com/api", alias="PORTALS_BASE_URL"
    )
    portals_auth: str = Field(default="", alias="PORTALS_AUTH")

    mrkt_base_url: str = Field(default="https://api.mrkt.land", alias="MRKT_BASE_URL")
    mrkt_auth: str = Field(default="", alias="MRKT_AUTH")

    tonnel_base_url: str = Field(
        default="https://gifts2.tonnel.network/api", alias="TONNEL_BASE_URL"
    )
    tonnel_auth: str = Field(default="", alias="TONNEL_AUTH")

    getgems_base_url: str = Field(
        default="https://api.getgems.io/public-api", alias="GETGEMS_BASE_URL"
    )
    getgems_api_key: str = Field(default="", alias="GETGEMS_API_KEY")

    # ------------------------------------------------------------------
    # TON
    # ------------------------------------------------------------------
    tonapi_base_url: str = Field(default="https://tonapi.io", alias="TONAPI_BASE_URL")
    tonapi_key: str = Field(default="", alias="TONAPI_KEY")
    #: Адрес кошелька для чтения баланса и сверки (read-only).
    ton_wallet_address: str = Field(default="", alias="TON_WALLET_ADDRESS")

    # ------------------------------------------------------------------
    # Торговые предохранители
    # ------------------------------------------------------------------
    #: Стартовый режим. SAFE = только рекомендации, ни одного write-вызова.
    default_mode: TradeMode = Field(default=TradeMode.SAFE, alias="DEFAULT_MODE")
    #: Глобальный аварийный стоп. True = ни одна write-операция не пройдёт.
    kill_switch: bool = Field(default=False, alias="KILL_SWITCH")
    #: Жёсткий потолок расходов за сутки, в Stars.
    daily_limit_stars: int = Field(default=0, alias="DAILY_LIMIT_STARS")
    #: Максимальная цена одной покупки, в Stars.
    max_trade_stars: int = Field(default=0, alias="MAX_TRADE_STARS")
    #: Максимум одновременно открытых позиций.
    max_open_positions: int = Field(default=10, alias="MAX_OPEN_POSITIONS")
    #: Минимальный чистый ROI (после комиссий) для кандидата, доля: 0.15 = 15%.
    min_roi: float = Field(default=0.15, alias="MIN_ROI")
    #: TTL бюджетного резерва в секундах.
    reservation_ttl_sec: int = Field(default=180, alias="RESERVATION_TTL_SEC")
    #: Какие площадки разрешены для AUTO (через запятую). Пусто = ни одной.
    auto_whitelist: str = Field(default="", alias="AUTO_WHITELIST")

    # ------------------------------------------------------------------
    # Воркеры
    # ------------------------------------------------------------------
    scan_interval_sec: int = Field(default=60, alias="SCAN_INTERVAL_SEC")
    reprice_interval_sec: int = Field(default=300, alias="REPRICE_INTERVAL_SEC")
    reconcile_interval_sec: int = Field(default=120, alias="RECONCILE_INTERVAL_SEC")

    # ------------------------------------------------------------------
    # Веб-панель
    # ------------------------------------------------------------------
    web_host: str = Field(default="127.0.0.1", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")
    web_user: str = Field(default="admin", alias="WEB_USER")
    web_password: str = Field(default="", alias="WEB_PASSWORD")
    public_url: str = Field(default="", alias="PUBLIC_URL")

    # ------------------------------------------------------------------
    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(str(v)).expanduser() if v else v

    @property
    def owner_id_list(self) -> list[int]:
        """Список Telegram id владельцев. Пустой = доступ закрыт всем."""
        out: list[int] = []
        for chunk in str(self.owner_ids).replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                out.append(int(chunk))
        return out

    @property
    def auto_markets(self) -> set[str]:
        """Площадки, которым разрешён режим AUTO."""
        return {
            c.strip().lower()
            for c in str(self.auto_whitelist).replace(";", ",").split(",")
            if c.strip()
        }

    @property
    def session_path(self) -> Path:
        """Полный путь к файлу Telethon-сессии."""
        return self.data_dir / f"{self.tg_session_name}.session"

    def ensure_dirs(self) -> None:
        """Создать рабочие каталоги, если их нет."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек."""
    return Settings()


settings = get_settings()
