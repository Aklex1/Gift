#!/usr/bin/env bash
# Обновление Gift до свежей версии из git.
set -euo pipefail
APP_DIR=/opt/gift
BRANCH="${BRANCH:-main}"

echo "==> Останавливаю сервисы"
systemctl stop gift-bot gift-worker gift-web || true

echo "==> Забираю код"
git -C "$APP_DIR" fetch --all --quiet
git -C "$APP_DIR" checkout "$BRANCH" --quiet
git -C "$APP_DIR" pull --quiet
chown -R gift:gift "$APP_DIR"

echo "==> Обновляю зависимости"
sudo -u gift "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Обновляю схему БД"
sudo -u gift env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli init

echo "==> Запускаю сервисы"
systemctl start gift-bot gift-worker gift-web
systemctl --no-pager status gift-bot gift-worker gift-web | head -30
