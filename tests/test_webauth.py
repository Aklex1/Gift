"""Тесты автопродления токенов площадок.

Токен площадки — это initData мини-приложения, он живёт часы. Здесь
проверяется, что бот берёт свежую строку сам, не дёргает Telegram без
нужды и не затирает рабочий токен, когда продление не удалось.
"""

from __future__ import annotations

import time

import pytest

from app.enums import Market
from app.services import webauth


@pytest.fixture(autouse=True)
def isolated_secrets(session, monkeypatch):
    """Подменить хранилище настроек на тестовое."""
    from contextlib import contextmanager

    from app.services import secrets

    @contextmanager
    def scope():
        yield session
        session.flush()

    monkeypatch.setattr(secrets, "session_scope", scope)
    secrets.invalidate()
    yield
    secrets.invalidate()


def _init_data(age_sec: int = 0) -> str:
    """Строка initData с нужным возрастом."""
    issued = int(time.time()) - age_sec
    return f"query_id=AAH123&user=%7B%22id%22%3A1%7D&auth_date={issued}&hash=abc"


# --- разбор URL от Telegram ------------------------------------------


def test_extracts_init_data_from_fragment():
    """Строка лежит во фрагменте и закодирована дважды."""
    url = (
        "https://portals.tg/#tgWebAppData=query_id%3DAAH%26auth_date%3D171"
        "&tgWebAppVersion=7.0&tgWebAppPlatform=android"
    )
    assert webauth.extract_init_data(url) == "query_id=AAH&auth_date=171"


def test_extracts_init_data_from_query():
    """Некоторые приложения получают параметры в query, а не во фрагменте."""
    url = "https://mrkt.tg/?tgWebAppData=query_id%3DBBB&x=1"
    assert webauth.extract_init_data(url) == "query_id=BBB"


def test_missing_init_data_is_empty():
    """Нет данных — пустая строка, а не исключение и не мусор."""
    assert webauth.extract_init_data("https://portals.tg/#tgWebAppVersion=7.0") == ""
    assert webauth.extract_init_data("") == ""


# --- возраст токена ---------------------------------------------------


def test_auth_date_parsed_with_and_without_prefix():
    """Префикс tma у Portals не мешает прочитать метку времени."""
    raw = _init_data(0)
    assert webauth.auth_date(raw) > 0
    assert webauth.auth_date(f"tma {raw}") == webauth.auth_date(raw)


def test_age_of_missing_token_is_unknown():
    """Токена нет — возраст неизвестен, а не ноль."""
    assert webauth.age_seconds(Market.PORTALS) is None


def test_age_reflects_auth_date():
    """Возраст считается по auth_date внутри строки."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", f"tma {_init_data(3600)}")
    age = webauth.age_seconds(Market.PORTALS)

    assert age is not None
    assert 3590 <= age <= 3610


# --- продление --------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_token_is_not_renewed(monkeypatch):
    """Свежий токен не повод дёргать Telegram."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", f"tma {_init_data(60)}")
    called = False

    async def never(*_a, **_kw):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(webauth, "fetch_init_data", never)
    report = await webauth.ensure_fresh(Market.PORTALS)

    assert report["skipped"] is True
    assert called is False


@pytest.mark.asyncio
async def test_stale_token_is_renewed(monkeypatch):
    """Протухший токен заменяется новым и сохраняется."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", f"tma {_init_data(9 * 3600)}")
    fresh = _init_data(0)

    async def fake(*_a, **_kw):
        return fresh

    monkeypatch.setattr(webauth, "fetch_init_data", fake)
    report = await webauth.ensure_fresh(Market.PORTALS)

    assert report["ok"] is True
    assert not report.get("skipped")
    # Portals ждёт строку с префиксом tma.
    assert secrets.resolve("PORTALS_AUTH", "") == f"tma {fresh}"


@pytest.mark.asyncio
async def test_failed_renewal_keeps_old_token(monkeypatch):
    """Сбой продления не стирает рабочий токен."""
    from app.services import secrets

    old = f"tma {_init_data(9 * 3600)}"
    secrets.set_value("PORTALS_AUTH", old)

    async def boom(*_a, **_kw):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(webauth, "fetch_init_data", boom)
    report = await webauth.ensure_fresh(Market.PORTALS)

    assert report["ok"] is False
    assert "Telegram недоступен" in report["detail"]
    assert secrets.resolve("PORTALS_AUTH", "") == old


@pytest.mark.asyncio
async def test_mrkt_renewal_drops_stale_exchange_token(monkeypatch):
    """У MRKT initData меняется на токен: старый токен надо сбросить."""
    from app.services import secrets

    secrets.set_value("MRKT_AUTH", "протухший-jwt")

    async def fake(*_a, **_kw):
        return _init_data(0)

    monkeypatch.setattr(webauth, "fetch_init_data", fake)
    await webauth.renew(Market.MRKT)

    assert secrets.resolve("MRKT_AUTH", "") == ""
    # MRKT принимает initData без префикса.
    assert not secrets.resolve("MRKT_INIT_DATA", "").startswith("tma ")


@pytest.mark.asyncio
async def test_token_without_auth_date_is_renewed(monkeypatch):
    """Строка без метки времени считается ненадёжной и обновляется."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", "tma query_id=AAH&hash=abc")
    fresh = _init_data(0)

    async def fake(*_a, **_kw):
        return fresh

    monkeypatch.setattr(webauth, "fetch_init_data", fake)
    report = await webauth.ensure_fresh(Market.PORTALS)

    assert not report.get("skipped")
    assert secrets.resolve("PORTALS_AUTH", "") == f"tma {fresh}"


# --- адрес мини-приложения -------------------------------------------


def test_default_miniapp_used():
    """По умолчанию — известный адрес площадки."""
    assert webauth._miniapp(Market.PORTALS) == ("portals", "market")


def test_miniapp_override():
    """Переезд площадки лечится настройкой, а не правкой кода."""
    from app.services import secrets

    secrets.set_value("PORTALS_MINIAPP", "@newportals:shop")
    assert webauth._miniapp(Market.PORTALS) == ("newportals", "shop")


def test_partial_override_keeps_default_short_name():
    """Указали только бота — короткое имя остаётся прежним."""
    from app.services import secrets

    secrets.set_value("PORTALS_MINIAPP", "newportals")
    assert webauth._miniapp(Market.PORTALS) == ("newportals", "market")


# --- два способа открыть приложение ----------------------------------


class _FakeGateway:
    """Шлюз, который отвечает по сценарию на каждый вызов."""

    def __init__(self, outcomes, *, peer=None):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.requests = []
        # Настоящий бот разрешается в InputPeerUser; подменять его
        # заглушкой нельзя — именно на приведении к InputUser и ломалось.
        from telethon.tl.types import InputPeerUser

        self.peer = peer or InputPeerUser(user_id=777, access_hash=123)

    async def client(self):
        """Клиент, умеющий разрешать имя бота."""
        peer = self.peer

        class _Client:
            async def get_input_entity(self, _name):
                return peer

        return _Client()

    async def call(self, request):
        """Отдать следующий запланированный исход."""
        self.calls += 1
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class _Result:
            url = outcome

        return _Result()


@pytest.fixture()
def fake_tg(monkeypatch):
    """Подменить шлюз Telegram заданным сценарием."""

    def install(outcomes, *, peer=None):
        gateway = _FakeGateway(outcomes, peer=peer)
        from app.adapters import telegram_gateway

        monkeypatch.setattr(telegram_gateway, "default_gateway", lambda: gateway)
        return gateway

    return install


@pytest.mark.asyncio
async def test_first_attempt_wins(fake_tg):
    """Если короткое имя верное, второй способ не нужен."""
    gateway = fake_tg(["https://portals.tg/#tgWebAppData=query_id%3DAAA"])

    assert await webauth.fetch_init_data(Market.PORTALS) == "query_id=AAA"
    assert gateway.calls == 1


@pytest.mark.asyncio
async def test_falls_back_to_menu_button(fake_tg):
    """Неверное короткое имя не ломает продление: есть кнопка меню."""
    gateway = fake_tg(
        [
            RuntimeError("BOT_APP_INVALID"),
            "https://portals.tg/#tgWebAppData=query_id%3DBBB",
        ]
    )

    assert await webauth.fetch_init_data(Market.PORTALS) == "query_id=BBB"
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_empty_url_counts_as_failure(fake_tg):
    """Ответ без tgWebAppData — повод попробовать второй способ."""
    gateway = fake_tg(
        ["https://portals.tg/#tgWebAppVersion=7.0", "https://x/#tgWebAppData=q%3D1"]
    )

    assert await webauth.fetch_init_data(Market.PORTALS) == "q=1"
    assert gateway.calls == 2


@pytest.mark.asyncio
async def test_both_attempts_failed_explains_why(fake_tg):
    """Когда не вышло ничего, в ошибке видно обе причины и что делать."""
    fake_tg([RuntimeError("BOT_APP_INVALID"), RuntimeError("USER_BOT_INVALID")])

    with pytest.raises(ValueError) as exc:
        await webauth.fetch_init_data(Market.PORTALS)

    text = str(exc.value)
    assert "BOT_APP_INVALID" in text
    assert "USER_BOT_INVALID" in text
    assert "PORTALS_MINIAPP" in text


# --- продление по факту отказа площадки ------------------------------


@pytest.mark.asyncio
async def test_portals_renews_on_401_and_retries(monkeypatch):
    """401 от Portals продлевает токен и повторяет запрос один раз."""
    from app.adapters.base import AuthRequired
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma старый")
    attempts = []

    async def fake_super(self, method, path, **kwargs):
        """Первый раз отказ, второй — успех."""
        attempts.append(self.auth)
        if len(attempts) == 1:
            raise AuthRequired("portals: нет доступа (401)")
        return {"ok": True}

    monkeypatch.setattr(
        "app.adapters.http_base.HttpMarketAdapter.request", fake_super
    )

    async def renewed(_market):
        from app.services import secrets

        secrets.set_value("PORTALS_AUTH", "tma новый")
        return {"ok": True, "market": "portals", "detail": "обновлён"}

    monkeypatch.setattr(webauth, "renew", renewed)

    assert await adapter.request("GET", "/nfts/search") == {"ok": True}
    assert attempts == ["tma старый", "tma новый"]


@pytest.mark.asyncio
async def test_portals_does_not_loop_on_repeated_401(monkeypatch):
    """Если и после продления 401, запрос падает, а не крутится вечно."""
    from app.adapters.base import AuthRequired
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma старый")
    calls = 0

    async def always_401(self, method, path, **kwargs):
        nonlocal calls
        calls += 1
        raise AuthRequired("portals: нет доступа (401)")

    monkeypatch.setattr(
        "app.adapters.http_base.HttpMarketAdapter.request", always_401
    )

    async def renewed(_market):
        return {"ok": True, "market": "portals", "detail": "обновлён"}

    monkeypatch.setattr(webauth, "renew", renewed)

    with pytest.raises(AuthRequired):
        await adapter.request("GET", "/nfts/search")
    # Исходный запрос и ровно одна повторная попытка.
    assert calls == 2


@pytest.mark.asyncio
async def test_portals_renewal_cooldown_expires(monkeypatch):
    """Пауза между продлениями истекает: одна неудача не выключает его навсегда."""
    from app.adapters.base import AuthRequired
    from app.adapters import portals as portals_mod
    from app.adapters.portals import PortalsAdapter

    monkeypatch.setattr(portals_mod, "RENEW_COOLDOWN_SEC", 0.0)
    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma старый")
    renewals = 0

    async def always_401(self, method, path, **kwargs):
        raise AuthRequired("portals: нет доступа (401)")

    monkeypatch.setattr(
        "app.adapters.http_base.HttpMarketAdapter.request", always_401
    )

    async def renewed(_market):
        nonlocal renewals
        renewals += 1
        return {"ok": True, "market": "portals", "detail": "обновлён"}

    monkeypatch.setattr(webauth, "renew", renewed)

    for _ in range(2):
        with pytest.raises(AuthRequired):
            await adapter.request("GET", "/nfts/search")

    # Оба запроса получили попытку продления, а не только первый.
    assert renewals == 2


@pytest.mark.asyncio
async def test_portals_failed_renewal_raises_original(monkeypatch):
    """Не удалось продлить — наружу идёт исходный отказ доступа."""
    from app.adapters.base import AuthRequired
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma старый")

    async def always_401(self, method, path, **kwargs):
        raise AuthRequired("portals: нет доступа (401)")

    monkeypatch.setattr(
        "app.adapters.http_base.HttpMarketAdapter.request", always_401
    )

    async def failed(_market):
        return {"ok": False, "market": "portals", "detail": "Telegram недоступен"}

    monkeypatch.setattr(webauth, "renew", failed)

    with pytest.raises(AuthRequired):
        await adapter.request("GET", "/nfts/search")


# --- приведение типов, на котором всё ломалось -----------------------


@pytest.mark.asyncio
async def test_nested_bot_id_is_input_user(fake_tg):
    """bot_id внутри InputBotAppShortName должен быть InputUser.

    Telethon приводит к Input*-виду только поля верхнего уровня.
    Вложенный bot_id уходил на сервер объектом User, и Telegram
    отвечал BOT_APP_BOT_INVALID.
    """
    from telethon.tl.types import InputUser

    gateway = fake_tg(["https://portals.tg/#tgWebAppData=query_id%3DAAA"])
    await webauth.fetch_init_data(Market.PORTALS)

    request = gateway.requests[0]
    assert isinstance(request.app.bot_id, InputUser)
    assert request.app.bot_id.user_id == 777
    assert request.app.short_name == "market"


@pytest.mark.asyncio
async def test_menu_button_bot_is_input_user(fake_tg):
    """У запасного способа поле bot тоже должно быть InputUser."""
    from telethon.tl.types import InputUser

    gateway = fake_tg(
        [RuntimeError("BOT_APP_BOT_INVALID"), "https://x/#tgWebAppData=q%3D1"]
    )
    await webauth.fetch_init_data(Market.PORTALS)

    assert isinstance(gateway.requests[1].bot, InputUser)


@pytest.mark.asyncio
async def test_non_bot_username_explained(fake_tg):
    """Канал вместо бота — понятная ошибка, а не BOT_APP_BOT_INVALID.

    Ровно с этим сталкивается человек, указавший в настройке имя
    канала: Telegram отвечает загадочно, и без пояснения непонятно,
    что именно исправлять.
    """
    from telethon.tl.types import InputPeerChannel

    fake_tg([], peer=InputPeerChannel(channel_id=42, access_hash=1))

    with pytest.raises(ValueError) as exc:
        await webauth.fetch_init_data(Market.PORTALS)

    text = str(exc.value)
    assert "не бот" in text
    assert "PORTALS_MINIAPP" in text


# --- честный разбор состояния токена ---------------------------------


def test_absent_token_reported_as_absent():
    """Когда токена нет, так и сказано."""
    state = webauth.token_state(Market.PORTALS)

    assert state["present"] is False
    assert state["note"] == "не задан"
    assert state["masked"] == "—"


def test_manual_token_is_not_called_missing():
    """Токен, вставленный руками, не должен показываться как отсутствующий.

    В нём нет auth_date, но это не повод утверждать, что его нет:
    человек видел такое сообщение сразу после того, как вставил
    рабочий токен из DevTools.
    """
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", "tma query_id=AAH&user=%7B%7D&hash=abcdef")
    state = webauth.token_state(Market.PORTALS)

    assert state["present"] is True
    assert state["age_min"] is None
    assert "вручную" in state["note"]
    # Значение показывается замаскированным, а не целиком.
    assert "query_id" not in state["masked"]
    assert state["masked"] != "—"


def test_dated_token_reports_age():
    """У токена с меткой времени показывается возраст."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", f"tma {_init_data(1800)}")
    state = webauth.token_state(Market.PORTALS)

    assert state["present"] is True
    assert 29 <= state["age_min"] <= 31
    assert state["stale"] is False


def test_old_token_flagged_stale():
    """Старый токен помечается как требующий продления."""
    from app.services import secrets

    secrets.set_value("PORTALS_AUTH", f"tma {_init_data(9 * 3600)}")

    assert webauth.token_state(Market.PORTALS)["stale"] is True


@pytest.mark.asyncio
async def test_first_renewal_not_blocked_by_cooldown(monkeypatch):
    """Первое продление не должно попадать под паузу.

    Отметка «когда продлевали» хранилась нулём, а time.monotonic()
    считается не от запуска процесса: «ноль» оказывался недавним
    моментом, и первые две минуты жизни процесса продление по 401
    молча не срабатывало.
    """
    from app.adapters.base import AuthRequired
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma старый")
    assert adapter._renewed_at is None, "до первой попытки отметки быть не должно"

    attempts = []

    async def fake_super(self, method, path, **kwargs):
        attempts.append(self.auth)
        if len(attempts) == 1:
            raise AuthRequired("portals: нет доступа (401)")
        return {"ok": True}

    monkeypatch.setattr(
        "app.adapters.http_base.HttpMarketAdapter.request", fake_super
    )

    renewed = []

    async def renew(_market):
        renewed.append(True)
        return {"ok": True, "market": "portals", "detail": "обновлён"}

    monkeypatch.setattr(webauth, "renew", renew)

    await adapter.request("GET", "/nfts/search")

    assert renewed, "первое продление обязано состояться"
    assert adapter._renewed_at is not None
