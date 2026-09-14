#!/usr/bin/env bash
# =====================================================================
#  Обновление Gift до свежей версии из git.
#
#  Переустанавливает не только код, но и юниты systemd с конфигом
#  nginx: иначе после смены портов в новой версии они разъезжаются
#  с приложением и nginx отдаёт 502.
#
#  Порты НЕ меняются: берутся из уже установленного юнита.
#
#     APP_DIR=/opt/gift APP_NAME=gift bash update.sh
# =====================================================================
set -euo pipefail

APP_NAME="${APP_NAME:-gift}"
APP_DIR="${APP_DIR:-/opt/gift}"
DATA_DIR="${DATA_DIR:-/var/lib/gift}"
APP_USER="${APP_USER:-$APP_NAME}"
BRANCH="${BRANCH:-claude/telegram-gift-resale-bot-p74jn7}"

UNIT_WEB="/etc/systemd/system/$APP_NAME-web.service"
NGINX_SITE="/etc/nginx/sites-available/$APP_NAME"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root"
[[ -d "$APP_DIR/.git" ]] || die "$APP_DIR не является git-клоном"

# --- порты берём из текущей установки, чтобы ничего не сломать ------
WEB_PORT="${WEB_PORT:-}"
if [[ -z "$WEB_PORT" && -f "$UNIT_WEB" ]]; then
    WEB_PORT="$(grep -oP '(?<=--port )\d+' "$UNIT_WEB" | head -1 || true)"
fi
WEB_PORT="${WEB_PORT:-8090}"

NGINX_PORT="${NGINX_PORT:-}"
if [[ -z "$NGINX_PORT" && -f "$NGINX_SITE" ]]; then
    NGINX_PORT="$(grep -oP '(?<=listen )\d+' "$NGINX_SITE" | head -1 || true)"
fi
NGINX_PORT="${NGINX_PORT:-8081}"

echo "Каталог      : $APP_DIR"
echo "Порт панели  : $NGINX_PORT (внутренний $WEB_PORT)"

# ---------------------------------------------------------------------
log "1/6 Останавливаю сервисы"
systemctl stop "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web" 2>/dev/null || true

log "2/6 Забираю код"
# Каталог принадлежит служебному пользователю, а git здесь работает
# от root — без этой пометки git отказывается с "dubious ownership".
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

git -C "$APP_DIR" fetch --all --quiet
git -C "$APP_DIR" checkout "$BRANCH" --quiet
git -C "$APP_DIR" pull --quiet
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

log "3/6 Обновляю зависимости"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

log "4/6 Дополняю .env новыми настройками"
# Установщик копирует .env.example только при первой установке.
# Без этого шага после обновления в рабочем файле не хватало ключей,
# появившихся в новой версии.
sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli env-sync

log "4b/6 Обновляю схему БД"
sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli init

log "5/6 Переустанавливаю сервисы и конфиг nginx"
render() {
    sed -e "s|__APP_DIR__|$APP_DIR|g" \
        -e "s|__DATA_DIR__|$DATA_DIR|g" \
        -e "s|__APP_USER__|$APP_USER|g" \
        -e "s|__SERVICE_PREFIX__|$APP_NAME|g" \
        -e "s|__WEB_PORT__|$WEB_PORT|g" \
        -e "s|__NGINX_PORT__|$NGINX_PORT|g" \
        "$1"
}
for unit in bot web worker; do
    render "$APP_DIR/deploy/gift-$unit.service" > "/etc/systemd/system/$APP_NAME-$unit.service"
    chmod 644 "/etc/systemd/system/$APP_NAME-$unit.service"
done
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
    "$APP_DIR/deploy/gift-cli" > "/usr/local/bin/$APP_NAME-cli"
chmod 755 "/usr/local/bin/$APP_NAME-cli"
systemctl daemon-reload

if [[ -f "$NGINX_SITE" ]] || command -v nginx >/dev/null; then
    render "$APP_DIR/deploy/nginx.conf" > "$NGINX_SITE"
    ln -sf "$NGINX_SITE" "/etc/nginx/sites-enabled/$APP_NAME"
    if nginx -t 2>/dev/null; then
        systemctl reload nginx
    else
        warn "Конфиг nginx отвергнут — проверьте: nginx -t"
    fi
fi

log "6/6 Запускаю сервисы"
systemctl start "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web"

# --- проверка, что панель действительно отвечает ---------------------
ok=0
for _ in $(seq 1 15); do
    sleep 1
    if curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/healthz" 2>/dev/null; then
        ok=1
        break
    fi
done

echo
if [[ "$ok" == "1" ]]; then
    IP="$(hostname -I | awk '{print $1}')"
    echo "✓ Панель отвечает: http://$IP:$NGINX_PORT/"
else
    warn "Панель не отвечает на 127.0.0.1:$WEB_PORT — вот почему:"
    systemctl --no-pager --lines=25 status "$APP_NAME-web" || true
    echo
    journalctl -u "$APP_NAME-web" -n 30 --no-pager || true
    exit 1
fi

systemctl --no-pager --lines=0 status \
    "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web" | grep -E "●|Active:" || true
