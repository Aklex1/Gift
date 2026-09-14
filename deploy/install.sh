#!/usr/bin/env bash
# =====================================================================
#  Установка Gift на чистый Ubuntu 24.04
#  Запуск от root:
#      bash deploy/install.sh
#  Скрипт идемпотентен: повторный запуск безопасен.
# =====================================================================
set -euo pipefail

APP_USER="gift"
APP_DIR="/opt/gift"
DATA_DIR="/var/lib/gift"
DB_NAME="gift"
DB_USER="gift"
REPO_URL="${REPO_URL:-https://github.com/Aklex1/gift.git}"
# Ветка по умолчанию. После слияния в main запускайте с BRANCH=main
BRANCH="${BRANCH:-claude/telegram-gift-resale-bot-p74jn7}"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root"

# ---------------------------------------------------------------------
log "1/8 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev \
    postgresql postgresql-contrib \
    nginx git curl build-essential libpq-dev ufw

# ---------------------------------------------------------------------
log "2/8 Пользователь и каталоги"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$DATA_DIR" "$DATA_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ---------------------------------------------------------------------
log "3/8 Код приложения"
if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch --all --quiet
    git -C "$APP_DIR" checkout "$BRANCH" --quiet
    git -C "$APP_DIR" pull --quiet
else
    rm -rf "$APP_DIR"
    git clone --branch "$BRANCH" --quiet "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------------
log "4/8 Виртуальное окружение"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------------
log "5/8 PostgreSQL"
systemctl enable --now postgresql
DB_PASS_FILE="$DATA_DIR/.dbpass"
if [[ -f "$DB_PASS_FILE" ]]; then
    DB_PASS="$(cat "$DB_PASS_FILE")"
else
    DB_PASS="$(openssl rand -hex 24)"
    umask 077; echo -n "$DB_PASS" > "$DB_PASS_FILE"
    chown "$APP_USER:$APP_USER" "$DB_PASS_FILE"
fi

sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1 \
  || sudo -u postgres psql -qc "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';"
sudo -u postgres psql -qc "ALTER ROLE $DB_USER PASSWORD '$DB_PASS';"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
  || sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"

# ---------------------------------------------------------------------
log "6/8 Конфигурация"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    SECRET="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
    WEBPASS="$(openssl rand -hex 12)"
    sed -i "s|^GIFT_SECRET_KEY=.*|GIFT_SECRET_KEY=$SECRET|"                     "$ENV_FILE"
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME|" "$ENV_FILE"
    sed -i "s|^WEB_PASSWORD=.*|WEB_PASSWORD=$WEBPASS|"                          "$ENV_FILE"
    sed -i "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|"                                 "$ENV_FILE"
    echo "$WEBPASS" > "$DATA_DIR/.webpass"
    chown "$APP_USER:$APP_USER" "$DATA_DIR/.webpass"
    NEW_ENV=1
else
    warn "Файл .env уже существует — оставляю как есть"
    NEW_ENV=0
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli init

# ---------------------------------------------------------------------
log "7/8 Сервисы systemd"
install -m 644 "$APP_DIR/deploy/gift-bot.service"    /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/gift-web.service"    /etc/systemd/system/
install -m 644 "$APP_DIR/deploy/gift-worker.service" /etc/systemd/system/
install -m 755 "$APP_DIR/deploy/gift-cli"            /usr/local/bin/gift-cli
systemctl daemon-reload

# ---------------------------------------------------------------------
log "8/8 Nginx и файрвол"
install -m 644 "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/gift
ln -sf /etc/nginx/sites-available/gift /etc/nginx/sites-enabled/gift
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

ufw allow OpenSSH >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
yes | ufw enable >/dev/null 2>&1 || true

# ---------------------------------------------------------------------
cat <<BANNER

=====================================================================
  Установка завершена.
=====================================================================

Сервисы НЕ запущены намеренно: бот без ключей работать не может,
а запускать торговлю до проверки настроек нельзя.

ДАЛЬШЕ — 4 ШАГА:

 1. Впишите ключи:
        nano $APP_DIR/.env
    Обязательно: BOT_TOKEN, OWNER_IDS, TG_API_ID, TG_API_HASH

 2. Авторизуйте торговый аккаунт Telegram (спросит код из Telegram):
        gift-cli login

 3. Проверьте конфигурацию:
        gift-cli doctor

 4. Запустите:
        systemctl enable --now gift-bot gift-web gift-worker
        systemctl status gift-bot --no-pager

Веб-панель:  http://$(hostname -I | awk '{print $1}')/
BANNER

if [[ "$NEW_ENV" == "1" ]]; then
cat <<CREDS
Логин панели: admin
Пароль:       $(cat "$DATA_DIR/.webpass")
CREDS
fi

cat <<'TAIL'

Логи:
    journalctl -u gift-bot -f
    journalctl -u gift-worker -f

Режим по умолчанию — SAFE: бот только показывает находки и
ничего не покупает, пока вы не переключите режим.
TAIL
