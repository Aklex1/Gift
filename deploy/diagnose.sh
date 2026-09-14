#!/usr/bin/env bash
# =====================================================================
#  Диагностика: почему панель или бот не работают.
#  Только чтение, ничего не меняет.
#     bash /opt/gift/deploy/diagnose.sh
# =====================================================================
set -uo pipefail

APP_NAME="${APP_NAME:-gift}"
APP_DIR="${APP_DIR:-/opt/gift}"
DATA_DIR="${DATA_DIR:-/var/lib/gift}"
UNIT_WEB="/etc/systemd/system/$APP_NAME-web.service"
NGINX_SITE="/etc/nginx/sites-available/$APP_NAME"

hdr() { printf '\n\033[1;36m── %s\033[0m\n' "$*"; }
ok()  { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad() { printf '  \033[1;31m✗\033[0m %s\n' "$*"; }
inf() { printf '    %s\n' "$*"; }

hdr "Сервисы"
for unit in bot web worker; do
    name="$APP_NAME-$unit"
    state="$(systemctl is-active "$name" 2>/dev/null)"
    boot="$(systemctl is-enabled "$name" 2>/dev/null)"
    if [[ "$state" == "active" ]]; then
        ok "$name: $state, автозапуск: $boot"
    else
        bad "$name: $state, автозапуск: $boot"
    fi
done

if [[ "$(systemctl is-enabled "$APP_NAME-web" 2>/dev/null)" != "enabled" ]]; then
    echo
    bad "Сервисы не включены — именно поэтому панель отдаёт 502."
    inf "Панели НЕ нужны ключи Telegram, её можно запустить прямо сейчас:"
    inf "    systemctl enable --now $APP_NAME-web"
    inf "Бот и воркер — после заполнения ключей."
fi

hdr "Порты"
WEB_PORT="$(grep -oP '(?<=--port )\d+' "$UNIT_WEB" 2>/dev/null | head -1)"
NGINX_PORT="$(grep -oP '(?<=listen )\d+' "$NGINX_SITE" 2>/dev/null | head -1)"
PROXY_PORT="$(grep -oP '(?<=127\.0\.0\.1:)\d+' "$NGINX_SITE" 2>/dev/null | head -1)"
inf "приложение слушает (из юнита) : ${WEB_PORT:-не найден}"
inf "nginx слушает                 : ${NGINX_PORT:-не найден}"
inf "nginx проксирует на           : ${PROXY_PORT:-не найден}"

if [[ -n "$WEB_PORT" && -n "$PROXY_PORT" ]]; then
    if [[ "$WEB_PORT" == "$PROXY_PORT" ]]; then
        ok "порты совпадают"
    else
        bad "ПОРТЫ РАЗЪЕХАЛИСЬ: приложение на $WEB_PORT, nginx шлёт на $PROXY_PORT"
        inf "это и есть причина 502. Лечится: bash $APP_DIR/deploy/update.sh"
    fi
fi

hdr "Кто занимает порты"
ss -ltnp 2>/dev/null | grep -E ":(${WEB_PORT:-0}|${NGINX_PORT:-0})\b" || inf "никто не слушает"

hdr "Ответ приложения напрямую"
if [[ -n "$WEB_PORT" ]] && curl -fsS -m 5 "http://127.0.0.1:$WEB_PORT/healthz" 2>/dev/null; then
    echo
    ok "приложение отвечает — значит проблема в nginx"
else
    bad "приложение не отвечает на 127.0.0.1:${WEB_PORT:-?}"
    inf "смотрите журнал ниже"
fi

hdr "Ответ через nginx"
if [[ -n "$NGINX_PORT" ]]; then
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://127.0.0.1:$NGINX_PORT/healthz" 2>/dev/null)"
    case "$code" in
        200) ok "nginx отдаёт 200" ;;
        502) bad "502 — nginx работает, приложение за ним не отвечает" ;;
        401) ok "401 на защищённой странице — это нормально" ;;
        *)   bad "код ответа: ${code:-нет ответа}" ;;
    esac
fi

hdr "Адреса площадок из конфигурации"
sudo -u "$APP_NAME" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" - <<'PYEOF' 2>/dev/null || inf "не удалось прочитать конфигурацию"
from app.config import settings

DEAD = {"portals-market.com", "api.mrkt.land"}
for name, url in [
    ("Portals", settings.portals_base_url),
    ("MRKT   ", settings.mrkt_base_url),
    ("Tonnel ", settings.tonnel_base_url),
]:
    mark = "x" if any(d in url for d in DEAD) else "v"
    print(f"  [{mark}] {name}: {url}")
if any(d in settings.portals_base_url or d in settings.mrkt_base_url for d in DEAD):
    print()
    print("  Адрес устарел и не резолвится. Исправить: gift-cli env-sync")
PYEOF

hdr "Сеть до площадок"
for host in api.telegram.org portals.tg api.tgmrkt.io tonapi.io; do
    if getent hosts "$host" >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$host" 2>/dev/null)"
        if [[ -n "$code" && "$code" != "000" ]]; then
            ok "$host — резолвится, HTTPS отвечает ($code)"
        else
            bad "$host — резолвится, но HTTPS не отвечает"
        fi
    else
        bad "$host — DNS не резолвит"
    fi
done
if ! getent hosts portals.tg >/dev/null 2>&1; then
    inf ""
    inf "DNS не работает. Проверьте:"
    inf "    cat /etc/resolv.conf"
    inf "    resolvectl status 2>/dev/null | head -20"
    inf "Быстрая проверка с публичным DNS:"
    inf "    getent hosts portals.tg || nslookup portals.tg 8.8.8.8"
fi

hdr "Конфигурация"
[[ -f "$APP_DIR/.env" ]] && ok ".env на месте" || bad ".env отсутствует: $APP_DIR/.env"
if [[ -f "$APP_DIR/.env" ]]; then
    grep -q '^GIFT_SECRET_KEY=.\+' "$APP_DIR/.env" && ok "GIFT_SECRET_KEY задан" || bad "GIFT_SECRET_KEY пуст"
    grep -q '^WEB_PASSWORD=.\+'    "$APP_DIR/.env" && ok "WEB_PASSWORD задан"    || bad "WEB_PASSWORD пуст — панель отключена"
    dups="$(grep -oE '^[A-Z_]+=' "$APP_DIR/.env" | sort | uniq -d | tr -d '=' | tr '\n' ' ')"
    if [[ -n "$dups" ]]; then
        bad "настройки заданы дважды: $dups"
        inf "работает последнее значение; лишние строки лучше убрать"
    fi
fi
[[ -f "$DATA_DIR/.webpass" ]] && inf "пароль панели: $(cat "$DATA_DIR/.webpass")"

hdr "База данных"
if sudo -u "$APP_NAME" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" \
     -c "from app.db import engine; engine.connect().close(); print('  ✓ подключение есть')" 2>/dev/null; then
    :
else
    bad "нет подключения к БД"
fi

hdr "Журнал панели (последние 25 строк)"
journalctl -u "$APP_NAME-web" -n 25 --no-pager 2>/dev/null || inf "журнал недоступен"

hdr "Журнал бота (последние 15 строк)"
journalctl -u "$APP_NAME-bot" -n 15 --no-pager 2>/dev/null || inf "журнал недоступен"

echo
echo "Если порты разъехались или сервис падает — почти всегда помогает:"
echo "    bash $APP_DIR/deploy/update.sh"
