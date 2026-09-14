"""Описание write-эндпоинтов площадки, задаваемое пользователем.

У Portals и MRKT нет публичной документации на покупку и продажу.
Угадывать такие эндпоинты в коде нельзя: ошибка в пути или в имени
поля означает потерю денег. Поэтому контракт выносится в JSON-файл,
который владелец заполняет по реальным запросам мини-приложения:

    /var/lib/gift/markets/portals.json

    {
      "buy": {
        "method": "POST",
        "path": "/nfts/buy",
        "body": {"nft_details": [{"id": "{external_id}", "price": "{price}"}]},
        "success_when": {"field": "status", "equals": "ok"}
      },
      "list": {
        "method": "POST",
        "path": "/nfts/{external_id}/list",
        "body": {"price": "{price}"}
      },
      "cancel": {
        "method": "POST",
        "path": "/nfts/{external_id}/unlist"
      }
    }

Подстановки в путях, телах и параметрах:
    {external_id} — идентификатор лота на площадке
    {price}       — цена в валюте площадки
    {price_nano}  — цена в нанотонах (целое число)

Пока файла нет или в нём нет нужной операции, соответствующая
возможность остаётся UNAVAILABLE, и адаптер физически не может
совершить сделку.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

NANO = Decimal("1000000000")

#: Операции, которые можно описать в контракте.
WRITE_OPS = ("buy", "list", "reprice", "cancel")

#: Маркер незаполненного пути в заготовке контракта.
PLACEHOLDER = "ЗАПОЛНИТЕ"


class ContractError(Exception):
    """Контракт площадки задан неверно."""


class Endpoint:
    """Один описанный вызов площадки."""

    def __init__(self, name: str, raw: dict) -> None:
        self.name = name
        self.method: str = str(raw.get("method", "POST")).upper()
        self.path: str = str(raw.get("path", ""))
        self.body: Any = raw.get("body")
        self.params: Any = raw.get("params")
        #: Правило успеха: {"field": "status", "equals": "ok"}.
        #: Без него успехом считается любой ответ 2xx.
        self.success_when: dict | None = raw.get("success_when")

        if not self.path:
            raise ContractError(f"{name}: не указан path")
        if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ContractError(f"{name}: недопустимый метод {self.method}")

    # ------------------------------------------------------------------
    def _subst(self, value: Any, mapping: dict[str, str]) -> Any:
        """Рекурсивно подставить значения в строки шаблона."""
        if isinstance(value, str):
            out = value
            for key, replacement in mapping.items():
                out = out.replace("{" + key + "}", replacement)
            return out
        if isinstance(value, dict):
            return {k: self._subst(v, mapping) for k, v in value.items()}
        if isinstance(value, list):
            return [self._subst(v, mapping) for v in value]
        return value

    def render(
        self, *, external_id: str = "", price: Decimal | None = None
    ) -> tuple[str, str, dict]:
        """Подготовить запрос.

        Returns:
            (метод, путь, kwargs для httpx)
        """
        price = price if price is not None else Decimal(0)
        mapping = {
            "external_id": str(external_id),
            "price": format(price.normalize(), "f"),
            "price_nano": str(int(price * NANO)),
        }
        path = self._subst(self.path, mapping)
        kwargs: dict = {}
        if self.body is not None:
            kwargs["json"] = self._subst(self.body, mapping)
        if self.params is not None:
            kwargs["params"] = self._subst(self.params, mapping)
        return (self.method, path, kwargs)

    def succeeded(self, response: Any) -> bool | None:
        """Признать ответ успешным.

        Returns:
            True/False, либо None — если по ответу судить нельзя
            (тогда исход считается неизвестным и уходит в сверку).
        """
        if not self.success_when:
            # Нет явного правила: 2xx уже получен, считаем успехом.
            return True
        field = self.success_when.get("field")
        expected = self.success_when.get("equals")
        if not field:
            return True
        actual: Any = response
        for part in str(field).split("."):
            if isinstance(actual, dict):
                actual = actual.get(part)
            else:
                return None
        if actual is None:
            return None
        return str(actual).lower() == str(expected).lower()


class MarketContract:
    """Набор write-эндпоинтов одной площадки."""

    def __init__(self, market: str, endpoints: dict[str, Endpoint]) -> None:
        self.market = market
        self.endpoints = endpoints

    def get(self, op: str) -> Endpoint | None:
        """Эндпоинт операции, если он описан."""
        return self.endpoints.get(op)

    def has(self, op: str) -> bool:
        """Описана ли операция."""
        return op in self.endpoints

    @property
    def described(self) -> list[str]:
        """Список описанных операций."""
        return sorted(self.endpoints)


def contract_path(market: str) -> Path:
    """Путь к файлу контракта площадки."""
    return settings.contracts_dir / f"{market}.json"


def load(market: str) -> MarketContract:
    """Прочитать контракт площадки.

    Отсутствие файла — не ошибка: площадка просто останется без
    боевых операций.
    """
    path = contract_path(market)
    if not path.exists():
        return MarketContract(market, {})

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Контракт %s повреждён: %s", path, exc)
        raise ContractError(f"{path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ContractError(f"{path}: ожидался объект JSON")

    endpoints: dict[str, Endpoint] = {}
    for op in WRITE_OPS:
        spec = raw.get(op)
        if spec is None:
            continue
        if not isinstance(spec, dict):
            raise ContractError(f"{path}: операция {op} должна быть объектом")
        endpoint = Endpoint(f"{market}.{op}", spec)
        if PLACEHOLDER in endpoint.path:
            # Незаполненная заготовка не должна открывать доступ к деньгам.
            log.warning(
                "Контракт %s: операция %s не заполнена (%s) — остаётся закрытой",
                path,
                op,
                PLACEHOLDER,
            )
            continue
        endpoints[op] = endpoint

    unknown = {k for k in raw if not k.startswith("_")} - set(WRITE_OPS)
    if unknown:
        log.warning(
            "Контракт %s: неизвестные операции проигнорированы: %s",
            path,
            ", ".join(sorted(unknown)),
        )
    return MarketContract(market, endpoints)


def write_template(market: str) -> Path:
    """Создать файл-заготовку контракта, если его ещё нет."""
    path = contract_path(market)
    if path.exists():
        return path
    settings.ensure_dirs()
    template = {
        "_комментарий": (
            "Заполните по реальным запросам мини-приложения "
            f"{market}: DevTools -> Network. Подстановки: "
            "{external_id}, {price}, {price_nano}. "
            "Операции без описания остаются запрещёнными."
        ),
        "buy": {
            "method": "POST",
            "path": "/ЗАПОЛНИТЕ/buy",
            "body": {"id": "{external_id}", "price": "{price}"},
            "success_when": {"field": "status", "equals": "ok"},
        },
        "list": {
            "method": "POST",
            "path": "/ЗАПОЛНИТЕ/{external_id}/list",
            "body": {"price": "{price}"},
        },
        "cancel": {
            "method": "POST",
            "path": "/ЗАПОЛНИТЕ/{external_id}/unlist",
        },
    }
    path.write_text(
        json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def is_placeholder(market: str) -> bool:
    """Остались ли в файле контракта незаполненные заглушки.

    Проверяется исходный файл, а не загруженный контракт: заглушки
    отбрасываются при загрузке и до эндпоинтов не доходят.
    """
    path = contract_path(market)
    if not path.exists():
        return False
    try:
        return PLACEHOLDER in path.read_text(encoding="utf-8")
    except OSError:
        return False
