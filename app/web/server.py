"""Веб-панель: дашборд, портфель, кандидаты, статус площадок.

Доступ закрыт HTTP Basic-аутентификацией. Панель предназначена для
наблюдения и базового управления; торговые подтверждения остаются
в Telegram-боте.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from app.adapters.registry import capability_matrix, probe_all
from app.config import settings
from app.db import session_scope
from app.enums import Market, TradeMode, display_currency
from app.logging_conf import setup_logging
from app.models import AuditLog, Budget, Candidate, Gift, Intent, Position, Strategy, utcnow
from app.services import budget as budget_service
from app.services import secrets as secrets_module
from app.services import gifts as gifts_service
from app.services import portfolio

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Валюта показывается человеку под своим нынешним именем: TON внутри —
# GRAM в интерфейсе. Фильтром, а не правкой каждой подстановки, чтобы
# название жило в одном месте.
templates.env.filters["cur"] = display_currency
security = HTTPBasic()

app = FastAPI(title="Gift — панель управления", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """HTTP Basic с защитой от тайминг-атак."""
    if not settings.web_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_PASSWORD не задан — панель отключена",
        )
    user_ok = secrets.compare_digest(credentials.username, settings.web_user)
    pass_ok = secrets.compare_digest(credentials.password, settings.web_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учётные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.on_event("startup")
async def on_startup() -> None:
    """Инициализация логов при старте."""
    from app.adapters import telegram_gateway

    setup_logging("web")
    # Файл MTProto-сессии держит воркер: панель обращается к Telegram
    # изредка и работает с копией ключа, иначе оба процесса упираются
    # в заблокированный SQLite.
    telegram_gateway.prefer_detached()
    log.info("Веб-панель запущена на %s:%s", settings.web_host, settings.web_port)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Проверка живости для мониторинга."""
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    """Главный дашборд."""
    with session_scope() as session:
        summary = portfolio.pnl_summary(session)
        budgets = [
            budget_service.snapshot(session, b.id) for b in session.query(Budget).all()
        ]
        strategies = session.query(Strategy).order_by(Strategy.priority.desc()).all()
        strategy_rows = [
            {
                "id": s.id,
                "name": s.name,
                "enabled": s.is_enabled,
                "mode": s.mode.value,
                "markets": ", ".join(s.markets or []),
                "min_roi": float(s.min_roi or 0) * 100,
                "max_risk": s.max_risk,
            }
            for s in strategies
        ]
        pending = (
            session.query(Candidate)
            .filter(Candidate.state == "pending", Candidate.expires_at > utcnow())
            .count()
        )

        # Балансы торговых аккаунтов: сколько денег реально доступно.
        from app.services import accounts as accounts_service

        account_rows = []
        total_stars = Decimal(0)
        total_ton = Decimal(0)
        for account in accounts_service.all_accounts(session):
            if not account.is_active:
                continue
            stars = Decimal(account.stars_balance or 0)
            ton = Decimal(account.ton_balance or 0)
            total_stars += stars
            total_ton += ton
            account_rows.append(
                {
                    "name": account.name,
                    "stars": account.stars_balance,
                    "ton": account.ton_balance,
                    "authorized": accounts_service.is_authorized(account),
                    "balance_at": account.balance_at,
                }
            )
        unknown = (
            session.query(Intent).filter(Intent.status == "unknown").count()
        )

    from app.services import balances, runtime

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "summary": summary,
            "budgets": budgets,
            "strategies": strategy_rows,
            "pending": pending,
            "unknown": unknown,
            "mode": runtime.mode().value,
            "kill_switch": runtime.kill_switch(),
            "accounts": account_rows,
            "markets": balances.snapshot(),
            "total_stars": total_stars,
            "total_ton": total_ton,
            "fmt": gifts_service.format_stars,
            "amount": gifts_service.format_amount,
        },
    )


@app.get("/candidates", response_class=HTMLResponse)
async def candidates_page(
    request: Request, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Список активных кандидатов и состояние сканера."""
    from app.services import scanner

    with session_scope() as session:
        rows = (
            session.query(Candidate)
            .filter(Candidate.state == "pending", Candidate.expires_at > utcnow())
            .order_by(Candidate.net_roi.desc())
            .limit(50)
            .all()
        )
        items = []
        for row in rows:
            gift = session.get(Gift, row.gift_id)
            items.append(
                {
                    "id": row.id,
                    "name": gifts_service.describe(gift) if gift else "?",
                    "market": str(row.market),
                    "price": Decimal(row.price_stars),
                    "fair": Decimal(row.fair_value_stars),
                    "roi": float(row.net_roi) * 100,
                    "risk": row.risk_score,
                    "confidence": str(row.confidence),
                    "rationale": row.rationale or {},
                }
            )
        # Отброшенные кандидаты объясняют, почему список пуст.
        recent_states = dict(
            session.query(Candidate.state, func.count(Candidate.id))
            .group_by(Candidate.state)
            .all()
        )

    report = scanner.last_report()
    stale = False
    age_sec = None
    if report:
        # Отчёт старше трёх интервалов означает, что воркер молчит.
        stamp = report.get("finished_at") or report.get("started_at")
        if stamp:
            try:
                age_sec = int(
                    (utcnow() - dt.datetime.fromisoformat(stamp)).total_seconds()
                )
                stale = age_sec > settings.scan_interval_sec * 3
            except (ValueError, TypeError):
                pass

    rejections = []
    if report:
        for key, count in sorted(
            (report.get("rejections") or {}).items(), key=lambda kv: -kv[1]
        ):
            rejections.append(
                {"label": scanner.REJECTION_LABELS.get(key, key), "count": count}
            )

    return templates.TemplateResponse(
        request=request,
        name="candidates.html",
        context={
            "items": items,
            "report": report,
            "stale": stale,
            "age_sec": age_sec,
            "rejections": rejections,
            "states": recent_states,
            "scan_interval": settings.scan_interval_sec,
            "fmt": gifts_service.format_stars,
        },
    )


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(
    request: Request, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Портфель: открытые и закрытые позиции."""
    with session_scope() as session:
        opened = portfolio.open_positions(session)
        open_rows = []
        for position in opened:
            gift = session.get(Gift, position.gift_id)
            open_rows.append(
                {
                    "id": position.id,
                    "name": gifts_service.describe(gift) if gift else "?",
                    "status": str(position.status),
                    "buy_price": Decimal(position.buy_price),
                    "list_price": (
                        Decimal(position.list_price) if position.list_price else None
                    ),
                    "bought_at": position.bought_at,
                }
            )
        closed = (
            session.query(Position)
            .filter(Position.status == "sold")
            .order_by(Position.sold_at.desc())
            .limit(50)
            .all()
        )
        closed_rows = []
        for position in closed:
            gift = session.get(Gift, position.gift_id)
            closed_rows.append(
                {
                    "id": position.id,
                    "name": gifts_service.describe(gift) if gift else "?",
                    "buy_price": Decimal(position.buy_price),
                    "sold_price": Decimal(position.sold_price or 0),
                    "pnl": position.realized_pnl or Decimal(0),
                    "sold_at": position.sold_at,
                }
            )
    return templates.TemplateResponse(
        request=request,
        name="portfolio.html",
        context={
            "open_rows": open_rows,
            "closed_rows": closed_rows,
            "fmt": gifts_service.format_stars,
        },
    )


@app.get("/markets", response_class=HTMLResponse)
async def markets_page(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    """Матрица возможностей площадок."""
    from app.adapters.base import Capability

    return templates.TemplateResponse(
        request=request,
        name="markets.html",
        context={
            "matrix": capability_matrix(),
            "capabilities": [c.value for c in Capability],
        },
    )


@app.post("/markets/probe")
async def markets_probe(_: str = Depends(require_auth)) -> JSONResponse:
    """Живая проверка доступности площадок (только чтение)."""
    report = await probe_all()
    return JSONResponse(report)


def _balance_totals(session) -> dict:
    """Суммарные балансы по активным аккаунтам."""
    from app.services import accounts as accounts_service

    stars = Decimal(0)
    ton = Decimal(0)
    for account in accounts_service.all_accounts(session):
        if not account.is_active:
            continue
        stars += Decimal(account.stars_balance or 0)
        ton += Decimal(account.ton_balance or 0)
    return {"stars": str(stars), "ton": str(ton)}


def _render_settings(
    request: Request, *, saved: int = 0, errors: dict[str, str] | None = None
) -> HTMLResponse:
    """Собрать страницу настроек."""
    from app.services import secrets

    state = secrets.masked_state()
    groups: dict[str, list] = {}
    for field in secrets.FIELDS:
        groups.setdefault(field.group, []).append(
            {"field": field, **state[field.key]}
        )

    session_ok = settings.session_path.exists()

    from app.services import webauth

    tokens = [webauth.token_state(market) for market in webauth.MINIAPPS]

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "groups": groups,
            "saved": saved,
            "errors": errors or {},
            "session_ok": session_ok,
            "session_path": str(settings.session_path),
            "tokens": tokens,
            "token_max_age_h": webauth.MAX_AGE_SEC // 3600,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, saved: int = 0, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Форма ввода ключей и токенов."""
    return _render_settings(request, saved=saved)


@app.post("/settings")
async def settings_save(request: Request, _: str = Depends(require_auth)):
    """Сохранить изменённые поля.

    Пустое поле означает «не менять»: иначе маскированное значение
    затирало бы сохранённый секрет. Для очистки есть отдельный флажок.

    Значения с явно неверным форматом не сохраняются: молча принятая
    опечатка в адресе кошелька или токене обнаружилась бы только
    в момент сделки.
    """
    from app.adapters.registry import _ADAPTERS
    from app.services import secrets

    form = await request.form()
    pending: list[tuple[str, str]] = []
    errors: dict[str, str] = {}

    for field in secrets.FIELDS:
        if form.get(f"clear__{field.key}"):
            pending.append((field.key, ""))
            continue
        value = str(form.get(field.key, "") or "").strip()
        if not value:
            continue
        error = secrets.validate(field.key, value)
        if error:
            errors[field.key] = error
            continue
        pending.append((field.key, value))

    if errors:
        # Ничего не сохраняем: пусть владелец увидит все ошибки разом.
        return _render_settings(request, errors=errors)

    for key, value in pending:
        secrets.set_value(key, value, actor="web")

    if pending:
        # Адаптеры создаются с токенами в конструкторе — пересоздаём.
        _ADAPTERS.clear()
    return RedirectResponse(f"/settings?saved={len(pending)}", status_code=303)


@app.post("/settings/test")
async def settings_test(_: str = Depends(require_auth)) -> JSONResponse:
    """Проверить доступность площадок с текущими ключами."""
    from app.adapters.registry import _ADAPTERS

    _ADAPTERS.clear()
    return JSONResponse(await probe_all())


@app.post("/settings/renew-tokens")
async def settings_renew_tokens(_: str = Depends(require_auth)) -> JSONResponse:
    """Продлить токены площадок через мини-приложения Telegram."""
    from app.adapters.registry import _ADAPTERS
    from app.services import webauth

    reports = await webauth.renew_all(force=True, detached=True)
    # Адаптеры держат старый токен в заголовках — пересоздаём.
    _ADAPTERS.clear()
    return JSONResponse({"reports": reports})


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(
    request: Request, saved: int = 0, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Переключатели боевого режима по площадкам."""
    from app.adapters.base import Capability
    from app.adapters.registry import get_adapter
    from app.services import runtime

    from app.services import arbitrage, fx, limits, notify

    state = runtime.snapshot()
    with session_scope() as session:
        daily = limits.snapshot(session)
        rates = fx.snapshot(session)
    markets = []
    for name, item in state["markets"].items():
        market = item["market"]
        adapter = get_adapter(market)
        contract = getattr(adapter, "contract", None)
        markets.append(
            {
                "key": name,
                "title": {
                    "telegram": "Telegram — официальный маркет",
                    "portals": "Portals",
                    "mrkt": "MRKT",
                }.get(name, name),
                "official": adapter.status_of(Capability.BUY).value == "supported",
                "enabled": item["write_enabled"],
                "cap": item["trade_cap"],
                "currency": item["currency"].value,
                "has_token": bool(getattr(adapter, "auth", None)) or name == "telegram",
                "operations": contract.described if contract else [],
                "daily": daily["markets"].get(name, {}),
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="trading.html",
        context={
            "mode": state["mode"].value,
            "kill_switch": state["kill_switch"],
            "experimental_auto": state["allow_experimental_auto"],
            "markets": markets,
            "daily_total": daily["total"],
            "fx": rates,
            "notify_kinds": notify.KINDS,
            "notify_enabled": notify.enabled_kinds(),
            "notify_ready": bool(
                secrets_module.resolve("BOT_TOKEN", settings.bot_token)
                and secrets_module.resolve("OWNER_IDS", settings.owner_ids).strip()
            ),
            "arb": {
                "enabled": arbitrage.enabled(),
                "min_roi_pct": arbitrage.min_roi() * 100,
                "transfer_ton": arbitrage.transfer_cost_ton(),
            },
            "saved": saved,
        },
    )


@app.post("/trading")
async def trading_save(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Применить изменения предохранителей.

    Каждое изменение пишется в журнал аудита: это те переключатели,
    что решают, тратятся деньги или нет.
    """
    from app.adapters.registry import _ADAPTERS
    from app.enums import Market, TradeMode
    from app.services import limits, runtime

    form = await request.form()
    changed = 0

    def _decimal(raw: object) -> Decimal | None:
        """Разобрать число из формы."""
        text = str(raw or "").strip().replace(",", ".")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None

    total_daily = _decimal(form.get("daily_total"))
    if total_daily is not None and total_daily != (
        limits.daily_total_limit() or Decimal(0)
    ):
        limits.set_daily_total_limit(total_daily)
        changed += 1

    raw_mode = str(form.get("mode") or "").strip().lower()
    if raw_mode:
        try:
            new_mode = TradeMode(raw_mode)
        except ValueError:
            new_mode = None
        if new_mode is not None and new_mode is not runtime.mode():
            runtime.set_mode(new_mode)
            changed += 1

    wanted_experimental = bool(form.get("experimental_auto"))
    if wanted_experimental != runtime.allow_experimental_auto():
        runtime.set_allow_experimental_auto(wanted_experimental)
        changed += 1

    for market in runtime.TRADABLE:
        wanted = bool(form.get(f"enable__{market.value}"))
        if wanted != runtime.write_enabled(market):
            runtime.set_write_enabled(market, wanted)
            changed += 1

        cap = _decimal(form.get(f"cap__{market.value}"))
        if cap is not None:
            if cap != (runtime.trade_cap(market) or Decimal(0)):
                runtime.set_trade_cap(market, cap)
                changed += 1

        daily = _decimal(form.get(f"daily__{market.value}"))
        if daily is not None and daily != (
            limits.daily_market_limit(market) or Decimal(0)
        ):
            limits.set_daily_market_limit(market, daily)
            changed += 1

    if changed:
        # Боевой режим влияет на набор возможностей адаптера.
        _ADAPTERS.clear()
    return RedirectResponse(f"/trading?saved={changed}", status_code=303)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(
    request: Request, saved: str = "", _: str = Depends(require_auth)
) -> HTMLResponse:
    """Торговые аккаунты Telegram с балансами."""
    from app.services import accounts as accounts_service

    with session_scope() as session:
        rows = []
        for account in accounts_service.all_accounts(session):
            rows.append(
                {
                    "id": account.id,
                    "name": account.name,
                    "api_id": account.api_id,
                    "phone": account.phone,
                    "username": account.tg_username,
                    "tg_user_id": account.tg_user_id,
                    "authorized": accounts_service.is_authorized(account),
                    "session_name": account.session_name,
                    "is_active": account.is_active,
                    "can_trade": account.can_trade,
                    "stars": account.stars_balance,
                    "ton": account.ton_balance,
                    "ton_address": account.ton_address,
                    "balance_at": account.balance_at,
                    "flood_until": account.flood_until,
                    "error": account.last_error,
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "accounts": rows,
            "saved": saved,
            "fmt": gifts_service.format_stars,
            "amount": gifts_service.format_amount,
        },
    )


@app.post("/accounts/add")
async def accounts_add(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Добавить торговый аккаунт."""
    from app.services import accounts as accounts_service

    form = await request.form()
    try:
        api_id = int(str(form.get("api_id") or "0").strip())
    except ValueError:
        return RedirectResponse("/accounts?saved=api_id-не-число", status_code=303)

    try:
        with session_scope() as session:
            account = accounts_service.create(
                session,
                name=str(form.get("name") or "").strip(),
                api_id=api_id,
                api_hash=str(form.get("api_hash") or "").strip(),
                phone=str(form.get("phone") or "").strip() or None,
                ton_address=str(form.get("ton_address") or "").strip() or None,
            )
            name = account.name
    except accounts_service.AccountError as exc:
        return RedirectResponse(f"/accounts?saved={exc}", status_code=303)

    return RedirectResponse(
        f"/accounts?saved=Аккаунт {name} добавлен. Войдите: gift-cli login --account {name}",
        status_code=303,
    )


@app.post("/accounts/{account_id}/toggle")
async def accounts_toggle(
    account_id: int, request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Включить/выключить аккаунт или право торговли."""
    from app.models import Account

    form = await request.form()
    field = str(form.get("field") or "")
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            return RedirectResponse("/accounts", status_code=303)
        if field == "active":
            account.is_active = not account.is_active
        elif field == "trade":
            account.can_trade = not account.can_trade
        session.add(
            AuditLog(
                actor="web",
                action=f"account.{field}",
                target=account.name,
                payload={"active": account.is_active, "can_trade": account.can_trade},
            )
        )
    return RedirectResponse("/accounts", status_code=303)


@app.post("/accounts/{account_id}/delete")
async def accounts_delete(
    account_id: int, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Удалить аккаунт вместе с файлом сессии."""
    from app.services import accounts as accounts_service

    with session_scope() as session:
        accounts_service.delete(session, account_id)
    from app.adapters import telegram_gateway

    telegram_gateway.forget(account_id)
    return RedirectResponse("/accounts?saved=Аккаунт удалён", status_code=303)


@app.post("/accounts/refresh")
async def accounts_refresh(_: str = Depends(require_auth)) -> RedirectResponse:
    """Опросить балансы всех аккаунтов."""
    from app.services import accounts as accounts_service

    from app.services import balances

    report = await accounts_service.refresh_balances()
    await balances.refresh()
    return RedirectResponse(
        f"/accounts?saved=Опрошено {report['checked']}, "
        f"успешно {report['ok']}, с ошибкой {report['failed']}",
        status_code=303,
    )


@app.post("/accounts/{account_id}/verify")
async def accounts_verify(
    account_id: int, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Проверить сессию аккаунта."""
    from app.services import accounts as accounts_service

    result = await accounts_service.verify_session(account_id)
    mark = "✓" if result.get("ok") else "✗"
    return RedirectResponse(
        f"/accounts?saved={mark} {result.get('detail')}", status_code=303
    )


@app.post("/strategies/{strategy_id}/roi")
async def strategies_quick_roi(
    strategy_id: int, request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Быстро поменять минимальный ROI, не открывая полную форму."""
    from app.models import Strategy

    form = await request.form()
    raw = str(form.get("min_roi") or "").strip().replace(",", ".")
    back = str(form.get("back") or "/")
    try:
        percent = Decimal(raw)
    except (InvalidOperation, ValueError):
        return RedirectResponse(back, status_code=303)

    with session_scope() as session:
        item = session.get(Strategy, strategy_id)
        if item is not None:
            item.min_roi = percent / 100
            session.add(
                AuditLog(
                    actor="web",
                    action="strategy.min_roi",
                    target=item.name,
                    payload={"min_roi": str(item.min_roi)},
                )
            )
    return RedirectResponse(back, status_code=303)


@app.get("/strategies", response_class=HTMLResponse)
async def strategies_page(
    request: Request, saved: str = "", _: str = Depends(require_auth)
) -> HTMLResponse:
    """Редактирование торговых стратегий."""
    from app.enums import Confidence, Market, TradeMode
    from app.models import Account, Budget, Strategy
    from app.services import runtime
    from app.services import strategy as strategy_service

    with session_scope() as session:
        accounts = [
            {"id": a.id, "name": a.name}
            for a in session.query(Account).order_by(Account.id).all()
        ]
        rows = []
        for item in (
            session.query(Strategy).order_by(Strategy.priority.desc()).all()
        ):
            budget = session.get(Budget, item.budget_id) if item.budget_id else None
            rows.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "enabled": item.is_enabled,
                    "mode": item.mode.value,
                    "priority": item.priority,
                    "markets": [str(m) for m in (item.markets or [])],
                    "collections": ", ".join(item.collections or []),
                    "models": ", ".join(item.models or []),
                    "min_price": item.min_price_stars,
                    "max_price": item.max_price_stars,
                    "min_roi": Decimal(item.min_roi or 0) * 100,
                    "max_risk": item.max_risk,
                    "min_confidence": item.min_confidence.value,
                    "sell_markup": Decimal(item.sell_markup or 0) * 100,
                    "reprice_step": Decimal(item.reprice_step or 0) * 100,
                    "reprice_cooldown_h": item.reprice_cooldown_h,
                    "floor_ratio": (Decimal(item.floor_ratio or 1) - 1) * 100,
                    "max_open_positions": item.max_open_positions,
                    "account_id": item.account_id,
                    "budget_cap": Decimal(budget.hard_cap) if budget else Decimal(0),
                    "budget_currency": budget.currency.value if budget else "STARS",
                    "budget_available": budget.available if budget else Decimal(0),
                    "open_positions": strategy_service.open_positions_count(
                        session, item.id
                    ),
                }
            )

    return templates.TemplateResponse(
        request=request,
        name="strategies.html",
        context={
            "strategies": rows,
            "accounts": accounts,
            "all_markets": [m.value for m in Market],
            "modes": [m.value for m in TradeMode],
            "confidences": [c.value for c in Confidence],
            "global_mode": runtime.mode().value,
            "saved": saved,
            "fmt": gifts_service.format_stars,
            "amount": gifts_service.format_amount,
        },
    )


@app.get("/feed", response_class=HTMLResponse)
async def feed_page(
    request: Request, saved: str = "", _: str = Depends(require_auth)
) -> HTMLResponse:
    """Канал находок: что удалось вычитать и куда это пошло."""
    from app.models import FeedFind
    from app.services import feed
    from app.services import strategy as strategy_service

    with session_scope() as session:
        scores = [s.as_dict() for s in feed.rank_collections(session)]
        recent = [
            {
                "collection": f.collection,
                "number": f.number,
                "price": f.price,
                "value": f.value,
                "realized": f.realized,
                "posted_at": f.posted_at,
            }
            for f in session.query(FeedFind)
            .order_by(FeedFind.posted_at.desc(), FeedFind.id.desc())
            .limit(30)
            .all()
        ]
        target = strategy_service.feed_strategy(session)
        strategy = (
            {
                "id": target.id,
                "name": target.name,
                "enabled": target.is_enabled,
                "collections": target.collections or [],
                "max_price": target.max_price_stars,
            }
            if target
            else None
        )

    return templates.TemplateResponse(
        request=request,
        name="feed.html",
        context={
            "channel": feed.channel_ref(),
            "last_sync": feed.last_sync(),
            "scores": scores,
            "recent": recent,
            "strategy": strategy,
            "saved": saved,
        },
    )


@app.post("/feed/sync")
async def feed_sync(_: str = Depends(require_auth)) -> RedirectResponse:
    """Прочитать канал прямо сейчас."""
    from app.services import feed
    from app.services import strategy as strategy_service

    if not feed.channel_ref():
        return RedirectResponse(
            "/feed?saved=Сначала укажите канал в «Настройках»", status_code=303
        )

    # detached: файл MTProto-сессии постоянно держит воркер, и работа
    # панели по тому же файлу упиралась в «database is locked».
    report = await feed.sync(detached=True)
    if report.get("error"):
        return RedirectResponse(f"/feed?saved={report['error']}", status_code=303)

    with session_scope() as session:
        if strategy_service.feed_strategy(session) is not None:
            strategy_service.refresh_feed_collections(session)

    return RedirectResponse(
        f"/feed?saved=Постов {report['posts']}, находок {report['finds']}, "
        f"новых {report['added']}",
        status_code=303,
    )


@app.post("/feed/strategy")
async def feed_strategy_toggle(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Включить или выключить стратегию канала."""
    from app.models import AuditLog
    from app.services import strategy as strategy_service

    form = await request.form()
    wanted = bool(form.get("enabled"))

    with session_scope() as session:
        target = strategy_service.ensure_feed_strategy(session)
        turned_off = strategy_service.set_enabled(session, target, wanted)
        if wanted:
            strategy_service.refresh_feed_collections(session)
        session.add(
            AuditLog(
                actor="web",
                action="strategy.feed",
                target=target.name,
                payload={"enabled": wanted, "turned_off": turned_off},
            )
        )

    note = "Стратегия канала включена" if wanted else "Стратегия канала выключена"
    if turned_off:
        note += f"; выключено: {', '.join(turned_off)}"
    return RedirectResponse(f"/feed?saved={note}", status_code=303)


@app.post("/strategies/new")
async def strategies_new(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Создать стратегию."""
    from app.services import strategy as strategy_service

    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/strategies?saved=Укажите имя", status_code=303)

    with session_scope() as session:
        strategy_service.create_strategy(session, name=name)
    return RedirectResponse(
        f"/strategies?saved=Стратегия {name} создана (выключена)", status_code=303
    )


@app.post("/strategies/{strategy_id}")
async def strategies_save(
    strategy_id: int, request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Сохранить параметры стратегии."""
    from app.enums import Confidence, TradeMode
    from app.models import Budget, Strategy
    from app.services import strategy as strategy_service

    form = await request.form()

    def num(field: str, default: Decimal | None = None) -> Decimal | None:
        """Число из формы; None, если поле пустое или неверное."""
        raw = str(form.get(field) or "").strip().replace(",", ".")
        if not raw:
            return default
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            return default

    def csv(field: str) -> list[str]:
        """Список значений через запятую."""
        raw = str(form.get(field) or "")
        return [x.strip() for x in raw.split(",") if x.strip()]

    with session_scope() as session:
        item = session.get(Strategy, strategy_id)
        if item is None:
            return RedirectResponse("/strategies", status_code=303)

        wanted = bool(form.get("enabled"))
        raw_mode = str(form.get("mode") or "").strip().lower()
        if raw_mode in {m.value for m in TradeMode}:
            item.mode = TradeMode(raw_mode)

        item.markets = form.getlist("markets") or []
        item.collections = csv("collections")
        item.models = csv("models")

        item.min_price_stars = num("min_price")
        item.max_price_stars = num("max_price")

        # Проценты в форме удобнее, внутри храним доли.
        roi = num("min_roi")
        if roi is not None:
            item.min_roi = roi / 100
        risk = num("max_risk")
        if risk is not None:
            item.max_risk = int(risk)
        raw_conf = str(form.get("min_confidence") or "").strip().lower()
        if raw_conf in {c.value for c in Confidence}:
            item.min_confidence = Confidence(raw_conf)

        markup = num("sell_markup")
        if markup is not None:
            item.sell_markup = markup / 100
        step = num("reprice_step")
        if step is not None:
            item.reprice_step = step / 100
        cooldown = num("reprice_cooldown_h")
        if cooldown is not None:
            item.reprice_cooldown_h = int(cooldown)
        floor = num("floor_ratio")
        if floor is not None:
            item.floor_ratio = Decimal(1) + floor / 100

        positions = num("max_open_positions")
        if positions is not None:
            item.max_open_positions = int(positions)
        priority = num("priority")
        if priority is not None:
            item.priority = int(priority)

        raw_account = str(form.get("account_id") or "").strip()
        item.account_id = int(raw_account) if raw_account.isdigit() else None

        budget = session.get(Budget, item.budget_id) if item.budget_id else None
        if budget is not None:
            cap = num("budget_cap")
            if cap is not None:
                budget.hard_cap = cap
            raw_currency = str(form.get("budget_currency") or "").strip().upper()
            if raw_currency in {"STARS", "TON"}:
                from app.enums import Currency

                budget.currency = Currency(raw_currency)

        # Включение при нулевом бюджете — частая ошибка: покупать не на что.
        if wanted and (budget is None or Decimal(budget.hard_cap) <= 0):
            item.is_enabled = False
            name = item.name
            session.add(
                AuditLog(actor="web", action="strategy.save", target=name, ok=False)
            )
            return RedirectResponse(
                f"/strategies?saved=Стратегия {name}: задайте бюджет, "
                f"иначе включать нечего",
                status_code=303,
            )

        # Взаимоисключение: стратегия канала сужает сканер до нескольких
        # коллекций, и параллельная стратегия вернула бы в выборку всё
        # остальное, сведя сужение к нулю.
        turned_off = strategy_service.set_enabled(session, item, wanted)

        name = item.name
        session.add(
            AuditLog(
                actor="web",
                action="strategy.save",
                target=name,
                payload={
                    "enabled": item.is_enabled,
                    "min_roi": str(item.min_roi),
                    "turned_off": turned_off,
                },
            )
        )

    note = f"Стратегия {name} сохранена"
    if turned_off:
        note += f"; выключено: {', '.join(turned_off)}"
    return RedirectResponse(f"/strategies?saved={note}", status_code=303)


@app.post("/strategies/{strategy_id}/delete")
async def strategies_delete(
    strategy_id: int, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Удалить стратегию."""
    from app.models import Strategy

    with session_scope() as session:
        item = session.get(Strategy, strategy_id)
        if item is not None:
            name = item.name
            session.delete(item)
            session.add(AuditLog(actor="web", action="strategy.delete", target=name))
    return RedirectResponse("/strategies?saved=Стратегия удалена", status_code=303)


@app.post("/trading/notify")
async def trading_notify(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Сохранить набор уведомлений и при желании прислать пробное."""
    from app.services import notify

    form = await request.form()
    notify.set_enabled_kinds(set(form.getlist("kinds")))

    if form.get("test"):
        ok = await notify.send(
            "🔔 <b>Проверка уведомлений</b>\n\n"
            "Если вы видите это сообщение, оповещения настроены верно."
        )
        mark = "отправлено" if ok else "не отправлено — проверьте токен и владельцев"
        return RedirectResponse(f"/trading?saved=Пробное сообщение {mark}", status_code=303)
    return RedirectResponse("/trading?saved=Уведомления сохранены", status_code=303)


@app.post("/trading/arbitrage")
async def trading_arbitrage(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Настроить поиск разницы цен между площадками."""
    from app.services import arbitrage, store

    form = await request.form()
    arbitrage.set_enabled(bool(form.get("enabled")))

    raw_roi = str(form.get("min_roi") or "").strip().replace(",", ".")
    if raw_roi:
        try:
            value = Decimal(raw_roi) / 100
            if 0 < value < 10:
                store.set(arbitrage.KEY_MIN_ROI, str(value))
        except (InvalidOperation, ValueError):
            pass

    raw_transfer = str(form.get("transfer_ton") or "").strip().replace(",", ".")
    if raw_transfer:
        try:
            value = Decimal(raw_transfer)
            if 0 <= value < 100:
                store.set(arbitrage.KEY_TRANSFER_TON, str(value))
        except (InvalidOperation, ValueError):
            pass

    return RedirectResponse("/trading?saved=Поиск разницы цен сохранён", status_code=303)


@app.post("/trading/fx")
async def trading_fx(
    request: Request, _: str = Depends(require_auth)
) -> RedirectResponse:
    """Сохранить параметры курса и обновить его."""
    from app.services import fx

    form = await request.form()

    raw_star = str(form.get("star_usd") or "").strip().replace(",", ".")
    if raw_star:
        try:
            fx.set_manual_star_usd(Decimal(raw_star))
        except (InvalidOperation, ValueError):
            pass
    elif form.get("clear_star"):
        fx.set_manual_star_usd(None)

    raw_spread = str(form.get("spread") or "").strip().replace(",", ".")
    if raw_spread:
        try:
            value = Decimal(raw_spread) / 100
            if 0 <= value < 1:
                fx.set_spread(value)
        except (InvalidOperation, ValueError):
            pass

    if form.get("refresh"):
        await fx.refresh()
    return RedirectResponse("/trading?saved=1", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request, _: str = Depends(require_auth)) -> HTMLResponse:
    """Журнал аудита."""
    with session_scope() as session:
        rows = (
            session.query(AuditLog).order_by(AuditLog.at.desc()).limit(200).all()
        )
        items = [
            {
                "at": row.at,
                "actor": row.actor,
                "action": row.action,
                "target": row.target,
                "ok": row.ok,
                "payload": row.payload,
            }
            for row in rows
        ]
    return templates.TemplateResponse(
        request=request, name="audit.html", context={"items": items}
    )


@app.post("/kill")
async def toggle_kill(_: str = Depends(require_auth)) -> RedirectResponse:
    """Переключить аварийный стоп.

    Значение пишется в общее хранилище, поэтому стоп немедленно
    действует и в воркере, и в боте.
    """
    from app.services import runtime

    runtime.set_kill_switch(not runtime.kill_switch(), actor="web")
    return RedirectResponse("/", status_code=303)


@app.get("/api/summary")
async def api_summary(_: str = Depends(require_auth)) -> JSONResponse:
    """Машинная сводка состояния."""
    from app.services import runtime

    with session_scope() as session:
        summary = portfolio.pnl_summary(session)
        return JSONResponse(
            {
                "closed_count": summary["closed_count"],
                "open_count": summary["open_count"],
                "realized_pnl": str(summary["realized_pnl"]),
                "roi": str(summary["roi"]),
                "balances": _balance_totals(session),
                "kill_switch": runtime.kill_switch(),
                "mode": runtime.mode().value,
            }
        )
