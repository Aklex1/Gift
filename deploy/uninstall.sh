#!/usr/bin/env bash
# Полное удаление Gift. Другие сайты на сервере не затрагиваются.
#     APP_DIR=/opt/gift APP_NAME=gift bash uninstall.sh
set -euo pipefail

APP_NAME="${APP_NAME:-gift}"
APP_DIR="${APP_DIR:-/opt/gift}"
DATA_DIR="${DATA_DIR:-/var/lib/gift}"
DB_NAME="${DB_NAME:-gift}"
DB_USER="${DB_USER:-gift}"

[[ $EUID -eq 0 ]] || { echo "Запустите от root" >&2; exit 1; }

read -rp "Удалить $APP_NAME вместе с базой и файлом сессии? [yes/NO] " ans
[[ "$ans" == "yes" ]] || { echo "Отменено"; exit 0; }

systemctl disable --now "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web" 2>/dev/null || true
rm -f "/etc/systemd/system/$APP_NAME-"{bot,web,worker}.service
systemctl daemon-reload

rm -f "/etc/nginx/sites-enabled/$APP_NAME" "/etc/nginx/sites-available/$APP_NAME"
nginx -t 2>/dev/null && systemctl reload nginx || true

sudo -u postgres dropdb --if-exists "$DB_NAME"
sudo -u postgres psql -qc "DROP ROLE IF EXISTS $DB_USER;"

rm -f "/usr/local/bin/$APP_NAME-cli"
rm -rf "$APP_DIR" "$DATA_DIR"
userdel "$APP_NAME" 2>/dev/null || true

echo "Удалено."
