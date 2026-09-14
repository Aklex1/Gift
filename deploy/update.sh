#!/usr/bin/env bash
# Обновление Gift до свежей версии из git.
# Параметры те же, что у install.sh:
#     APP_DIR=/opt/gift APP_NAME=gift bash update.sh
set -euo pipefail

APP_NAME="${APP_NAME:-gift}"
APP_DIR="${APP_DIR:-/opt/gift}"
APP_USER="${APP_USER:-$APP_NAME}"
BRANCH="${BRANCH:-claude/telegram-gift-resale-bot-p74jn7}"

[[ $EUID -eq 0 ]] || { echo "Запустите от root" >&2; exit 1; }

echo "==> Останавливаю сервисы"
systemctl stop "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web" || true

echo "==> Забираю код"
git -C "$APP_DIR" fetch --all --quiet
git -C "$APP_DIR" checkout "$BRANCH" --quiet
git -C "$APP_DIR" pull --quiet
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Обновляю зависимости"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Обновляю схему БД"
sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli init

echo "==> Запускаю сервисы"
systemctl start "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web"
systemctl --no-pager status "$APP_NAME-bot" "$APP_NAME-worker" "$APP_NAME-web" | head -40
