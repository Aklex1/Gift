"""Тесты пригодности токена площадки к отправке в HTTP-заголовке.

Токен — это initData мини-приложения, и Telegram отдаёт его с
процентным кодированием. DevTools показывает заголовки расшифрованными,
поэтому скопированное оттуда значение содержит живую кириллицу: имя
пользователя внутри `user=`. В заголовок она не проходит, httpx падает
с UnicodeEncodeError, и площадка отваливается целиком — именно так
Portals показывал ошибку вместо баланса.
"""

from __future__ import annotations

from urllib.parse import unquote

import httpx
import pytest

from app.adapters.http_base import ascii_header

# Токен в том виде, в каком его копируют из DevTools.
DECODED = (
    'tma query_id=AAHdF6IQAAAAAN0XohDhrOrc'
    '&user={"id":123,"first_name":"Валентина","username":"x"}'
    "&auth_date=1750000000&hash=abcdef"
)


def test_plain_token_untouched():
    """ASCII-токен не меняется: чинить нечего."""
    token = "tma query_id=AAH&auth_date=1&hash=abc"

    assert ascii_header(token) == token


def test_cyrillic_is_percent_encoded():
    """Кириллица кодируется обратно в тот вид, в каком её шлёт Telegram."""
    fixed = ascii_header(DECODED)

    assert fixed.isascii()
    assert "%D0%92" in fixed          # «В» из «Валентина»
    assert "Валентина" not in fixed


def test_result_fits_into_http_header():
    """Главное: httpx больше не падает.

    Без правки здесь поднимался UnicodeEncodeError, и баланс Portals
    не читался вовсе.
    """
    with pytest.raises(UnicodeEncodeError):
        httpx.Headers({"Authorization": DECODED})

    headers = httpx.Headers({"Authorization": ascii_header(DECODED)})
    assert headers["authorization"].startswith("tma ")


def test_round_trip_restores_original():
    """Сервер расшифрует заголовок обратно — подпись не ломается."""
    assert unquote(ascii_header(DECODED)) == DECODED


def test_ascii_parts_are_not_re_encoded():
    """Разделители и скобки остаются как есть: перекодировать их незачем."""
    fixed = ascii_header(DECODED)

    assert "&auth_date=1750000000" in fixed
    assert 'user={"id":123' in fixed


def test_empty_value():
    """Пустая строка не должна ломать разбор."""
    assert ascii_header("") == ""


# --- применение в адаптерах -------------------------------------------


def test_portals_header_is_sendable():
    """Portals собирает заголовки сам — правка должна доходить и туда."""
    from app.adapters.portals import PortalsAdapter

    adapter = PortalsAdapter(base_url="https://portals.tg/api", auth=DECODED)
    headers = adapter._headers()

    assert headers["Authorization"].isascii()
    httpx.Headers(headers)  # не должно бросить


def test_mrkt_header_is_sendable():
    """И MRKT тоже."""
    from app.adapters.mrkt import MrktAdapter

    adapter = MrktAdapter(base_url="https://api.tgmrkt.io/api/v1", auth=DECODED)
    headers = adapter._headers()

    assert headers["Authorization"].isascii()
    httpx.Headers(headers)


def test_portals_keeps_required_headers():
    """Починка заголовка не должна снести Origin и Referer."""
    from app.adapters.portals import PortalsAdapter

    headers = PortalsAdapter(
        base_url="https://portals.tg/api", auth=DECODED
    )._headers()

    assert headers["Origin"] == "https://portals.tg"
    assert headers["Referer"] == "https://portals.tg/"
