"""Веб-панель: дашборд, портфель, кандидаты, статус площадок.

Доступ закрыт HTTP Basic-аутентификацией. Панель предназначена для
наблюдения и базового управления; торговые подтверждения остаются
в Telegram-боте.
"""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.adapters.registry import capability_matrix, probe_all
from app.config import settings
from app.db import session_scope
from app.enums import Market, TradeMode
from app.logging_conf import setup_logging
from app.models import AuditLog, Budget, Candidate, Gift, Intent, Position, Strategy, utcnow
from app.services import budget as budget_service
from app.services import gifts as gifts_service
from app.services import portfolio

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
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
    setup_logging("web")
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
        unknown = (
            session.query(Intent).filter(Intent.status == "unknown").count()
        )

    from app.services import runtime

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
            "fmt": gifts_service.format_stars,
        },
    )


@app.get("/candidates", response_class=HTMLResponse)
async def candidates_page(
    request: Request, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Список активных кандидатов."""
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
    return templates.TemplateResponse(
        request=request,
        name="candidates.html",
        context={"items": items, "fmt": gifts_service.format_stars},
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
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "groups": groups,
            "saved": saved,
            "errors": errors or {},
            "session_ok": session_ok,
            "session_path": str(settings.session_path),
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


@app.get("/trading", response_class=HTMLResponse)
async def trading_page(
    request: Request, saved: int = 0, _: str = Depends(require_auth)
) -> HTMLResponse:
    """Переключатели боевого режима по площадкам."""
    from app.adapters.base import Capability
    from app.adapters.registry import get_adapter
    from app.services import runtime

    state = runtime.snapshot()
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
    from decimal import Decimal, InvalidOperation

    from app.adapters.registry import _ADAPTERS
    from app.enums import Market, TradeMode
    from app.services import runtime

    form = await request.form()
    changed = 0

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

        raw_cap = str(form.get(f"cap__{market.value}") or "").strip().replace(",", ".")
        if raw_cap:
            try:
                cap = Decimal(raw_cap)
            except (InvalidOperation, ValueError):
                continue
            if cap != (runtime.trade_cap(market) or Decimal(0)):
                runtime.set_trade_cap(market, cap)
                changed += 1

    if changed:
        # Боевой режим влияет на набор возможностей адаптера.
        _ADAPTERS.clear()
    return RedirectResponse(f"/trading?saved={changed}", status_code=303)


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
                "kill_switch": runtime.kill_switch(),
                "mode": runtime.mode().value,
            }
        )
