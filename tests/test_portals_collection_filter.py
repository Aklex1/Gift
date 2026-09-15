"""Тесты фильтра по коллекции у Portals.

Площадка не отвечает ошибкой на непонятное значение фильтра — она
молча отдаёт выборку без него. При сортировке «по возрастанию цены»
это самые дешёвые лоты **всего рынка**.

На боевом сервере это выглядело так. Запрос по Light Sword вернул лоты
по 3.95–4.00 GRAM с моделями Berry Shake, Gummy Bear, Vanilla Jam,
Pumpkin Spice — конфетные коллекции, floor рынка. Рядом с настоящими
ценами Light Sword в Telegram (около 715 Stars) это дало «связку» с
разницей 79% и доходностью +39.7%, которой не существует.

Тот же класс ошибки, что и у Fragment, и лечится так же: проверкой,
что фильтр применился. Поэтому здесь проверяется не формат запроса, а
отказ — молчание вместо выдуманной находки.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.adapters.base import SearchSkipped
from app.adapters.portals import PortalsAdapter, belongs_to


def _nft(collection, model, price, ident):
    """Запись Portals в том виде, в каком она приходит."""
    return {
        "id": ident,
        "name": collection,
        "price": str(price),
        "status": "listed",
        "attributes": [{"type": "model", "value": model}],
    }


def _adapter(rows, monkeypatch):
    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth="tma x")
    seen: dict = {}

    async def fake_request(method, path, **kwargs):
        if path == "/collections":
            return {
                "collections": [
                    {"id": "ls-1", "short_name": "lightsword",
                     "name": "Light Sword"},
                ]
            }
        seen.update(kwargs.get("params") or {})
        return {"results": rows}

    monkeypatch.setattr(adapter, "request", fake_request)
    return adapter, seen


# --- принадлежность лота -----------------------------------------------


def test_same_collection_matches():
    """Название с номером — это та же коллекция."""
    assert belongs_to("Light Sword", "Light Sword")
    assert belongs_to("Light Sword #1234", "Light Sword")
    assert belongs_to("lightsword", "Light Sword")


def test_another_collection_does_not_match():
    """Конфетная модель за 3.95 GRAM — не Light Sword."""
    assert not belongs_to("Lol Pop", "Light Sword")
    assert not belongs_to("Candy Cane", "Light Sword")
    assert not belongs_to(None, "Light Sword")


# --- поиск --------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_filters_by_collection_id(monkeypatch):
    """В фильтр уходит id коллекции: имя площадка не понимает."""
    adapter, seen = _adapter(
        [_nft("Light Sword", "Bifrost", "6.22", "a")], monkeypatch
    )

    await adapter.search(collection="Light Sword", limit=10)

    assert seen["collection_id"] == "ls-1"
    assert "filter_by_collections" not in seen


@pytest.mark.asyncio
async def test_unknown_collection_is_refused(monkeypatch):
    """Коллекции нет в каталоге — отказ, а не запрос без фильтра.

    Уйти без фильтра значит получить самые дешёвые лоты всего рынка и
    принять их за эту коллекцию.
    """
    adapter, _ = _adapter([], monkeypatch)

    with pytest.raises(SearchSkipped) as exc:
        await adapter.search(collection="Такой Нет", limit=10)

    assert "каталоге" in str(exc.value)


@pytest.mark.asyncio
async def test_catalog_is_asked_once(monkeypatch):
    """Каталог спрашивается один раз, а не на каждый поиск."""
    adapter, _ = _adapter(
        [_nft("Light Sword", "Bifrost", "6.22", "a")], monkeypatch
    )
    paths: list[str] = []
    inner = adapter.request

    async def counting(method, path, **kwargs):
        paths.append(path)
        return await inner(method, path, **kwargs)

    adapter.request = counting

    await adapter.search(collection="Light Sword", limit=10)
    await adapter.search(collection="Light Sword", limit=10)

    assert paths.count("/collections") == 1


@pytest.mark.asyncio
async def test_foreign_lots_are_refused_not_returned(monkeypatch):
    """Чужие лоты — это отказ, а не выдача.

    Вернуть их значило бы подставить floor всего рынка в оценку
    конкретной коллекции.
    """
    adapter, _ = _adapter(
        [
            _nft("Lol Pop", "Berry Shake", "3.95", "a"),
            _nft("Candy Cane", "Gummy Bear", "3.98", "b"),
        ],
        monkeypatch,
    )

    with pytest.raises(SearchSkipped) as exc:
        await adapter.search(collection="Light Sword", limit=10)

    assert "не применился" in str(exc.value)


@pytest.mark.asyncio
async def test_own_lots_pass_through(monkeypatch):
    """Свои лоты возвращаются как прежде."""
    adapter, _ = _adapter(
        [
            _nft("Light Sword", "Bifrost", "6.22", "a"),
            _nft("Light Sword #77", "Toy Blade", "6.12", "b"),
        ],
        monkeypatch,
    )

    rows = await adapter.search(collection="Light Sword", limit=10)

    assert [r.external_id for r in rows] == ["a", "b"]
    assert rows[0].price == Decimal("6.22")


@pytest.mark.asyncio
async def test_mixed_response_keeps_only_ours(monkeypatch):
    """Из смешанной выдачи остаются только лоты запрошенной коллекции."""
    adapter, _ = _adapter(
        [
            _nft("Lol Pop", "Berry Shake", "3.95", "a"),
            _nft("Light Sword", "Bifrost", "6.22", "b"),
        ],
        monkeypatch,
    )

    rows = await adapter.search(collection="Light Sword", limit=10)

    assert [r.external_id for r in rows] == ["b"]


@pytest.mark.asyncio
async def test_empty_answer_is_not_an_error(monkeypatch):
    """Пусто — это пусто. Отказ приберегается для подмены выдачи."""
    adapter, _ = _adapter([], monkeypatch)

    assert await adapter.search(collection="Light Sword", limit=10) == []


@pytest.mark.asyncio
async def test_search_without_a_collection_is_untouched(monkeypatch):
    """Обход без коллекции проверять нечем — и он не трогается."""
    adapter, _ = _adapter(
        [_nft("Lol Pop", "Berry Shake", "3.95", "a")], monkeypatch
    )

    rows = await adapter.search(limit=10)

    assert len(rows) == 1


# --- откуда берётся название коллекции ---------------------------------


def test_collection_comes_from_its_own_key():
    """Коллекция читается из `collection_name`, а не из `name`.

    В ответе Portals `name` — имя самого NFT. Пока разбор брал его
    первым, лот подписывался чужим названием, и проверка коллекции не
    могла работать в принципе.
    """
    from app.adapters.portals import PortalsAdapter

    gift = PortalsAdapter.parse_gift(
        {
            "id": "1",
            "name": "Light Sword #1234",
            "collection_name": "Light Sword",
            "attributes": [{"type": "model", "value": "Bifrost"}],
        }
    )

    assert gift.collection == "Light Sword"


def test_name_is_the_last_resort():
    """Без явного ключа остаётся `name` — лучше, чем ничего."""
    from app.adapters.portals import PortalsAdapter

    gift = PortalsAdapter.parse_gift({"id": "1", "name": "Light Sword"})

    assert gift.collection == "Light Sword"


@pytest.mark.asyncio
async def test_unnamed_lots_survive_the_check(monkeypatch):
    """Лот без названия коллекции не отбрасывается.

    Проверка ловит подмену выдачи, а не молчание площадки о названии.
    Иначе один изменённый ключ в ответе выкосил бы всю выдачу Portals,
    и это выглядело бы как «на площадке ничего нет».
    """
    adapter, _ = _adapter(
        [{"id": "a", "price": "6.22",
          "attributes": [{"type": "model", "value": "Bifrost"}]}],
        monkeypatch,
    )

    rows = await adapter.search(collection="Light Sword", limit=10)

    assert [r.external_id for r in rows] == ["a"]
