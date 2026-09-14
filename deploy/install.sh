#!/usr/bin/env bash
# =====================================================================
#  Установка Gift на Ubuntu 24.04
#
#  Ставится в отдельный каталог и НЕ трогает уже работающие на сервере
#  сайты: свой системный пользователь, своя база, свой порт nginx,
#  порт 80 не занимается и default-сайт не удаляется.
#
#  Запуск от root:
#      bash install.sh
#
#  Всё настраивается переменными окружения, например:
#      APP_DIR=/opt/gift NGINX_PORT=8081 bash install.sh
#
#  Скрипт идемпотентен: повторный запуск безопасен.
# =====================================================================
set -euo pipefail

# --- параметры установки (можно переопределить окружением) ------------
APP_NAME="${APP_NAME:-gift}"            # префикс сервисов и имя пользователя
APP_DIR="${APP_DIR:-/opt/gift}"         # куда положить код
DATA_DIR="${DATA_DIR:-/var/lib/gift}"   # сессия, логи, секреты
DB_NAME="${DB_NAME:-gift}"
DB_USER="${DB_USER:-gift}"
WEB_PORT="${WEB_PORT:-8090}"            # внутренний порт приложения (127.0.0.1)
NGINX_PORT="${NGINX_PORT:-8081}"        # внешний порт панели
SETUP_NGINX="${SETUP_NGINX:-1}"         # 0 = не трогать nginx вообще
REPO_URL="${REPO_URL:-https://github.com/Aklex1/Gift.git}"
BRANCH="${BRANCH:-claude/telegram-gift-resale-bot-p74jn7}"

APP_USER="$APP_NAME"

log()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите от root"

# --- проверка занятости портов ---------------------------------------
port_busy() { ss -ltn "sport = :$1" 2>/dev/null | grep -q LISTEN; }

log "0/8 Проверка окружения"
echo "    Каталог кода : $APP_DIR"
echo "    Данные       : $DATA_DIR"
echo "    Пользователь : $APP_USER"
echo "    База         : $DB_NAME"
echo "    Порт панели  : $NGINX_PORT (внутренний $WEB_PORT)"

if port_busy "$NGINX_PORT"; then
    die "Порт $NGINX_PORT уже занят. Запустите с другим: NGINX_PORT=8082 bash install.sh"
fi
if port_busy "$WEB_PORT"; then
    die "Порт $WEB_PORT уже занят. Запустите с другим: WEB_PORT=8091 bash install.sh"
fi
if [[ -e "$APP_DIR" && ! -d "$APP_DIR/.git" ]]; then
    die "$APP_DIR существует и это не git-клон. Укажите другой: APP_DIR=/opt/gift2 bash install.sh"
fi

# ---------------------------------------------------------------------
log "1/8 Системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-dev \
    postgresql postgresql-contrib \
    git curl build-essential libpq-dev iproute2
[[ "$SETUP_NGINX" == "1" ]] && apt-get install -y -qq nginx

# ---------------------------------------------------------------------
log "2/8 Пользователь и каталоги"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$DATA_DIR" "$DATA_DIR/logs"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ---------------------------------------------------------------------
log "3/8 Код приложения"
# Каталог принадлежит служебному пользователю, а git здесь работает
# от root — без этой пометки git отказывается с "dubious ownership".
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

if [[ -d "$APP_DIR/.git" ]]; then
    git -C "$APP_DIR" fetch --all --quiet
    git -C "$APP_DIR" checkout "$BRANCH" --quiet
    git -C "$APP_DIR" pull --quiet
else
    mkdir -p "$(dirname "$APP_DIR")"
    git clone --branch "$BRANCH" --quiet "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------------
log "4/8 Виртуальное окружение"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ---------------------------------------------------------------------
log "5/8 PostgreSQL (отдельная база, чужие не трогаем)"
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
NEW_ENV=0
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    SECRET="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"
    WEBPASS="$(openssl rand -hex 12)"
    sed -i "s|^GIFT_SECRET_KEY=.*|GIFT_SECRET_KEY=$SECRET|"  "$ENV_FILE"
    sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME|" "$ENV_FILE"
    sed -i "s|^WEB_PASSWORD=.*|WEB_PASSWORD=$WEBPASS|"       "$ENV_FILE"
    sed -i "s|^WEB_PORT=.*|WEB_PORT=$WEB_PORT|"              "$ENV_FILE"
    sed -i "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|"              "$ENV_FILE"
    echo "$WEBPASS" > "$DATA_DIR/.webpass"
    chown "$APP_USER:$APP_USER" "$DATA_DIR/.webpass"
    NEW_ENV=1
else
    warn "Файл .env уже существует — оставляю как есть"
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli init

# ---------------------------------------------------------------------
log "7/8 Сервисы systemd"
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

# ---------------------------------------------------------------------
if [[ "$SETUP_NGINX" == "1" ]]; then
    log "8/8 Nginx на порту $NGINX_PORT (порт 80 не трогаем)"
    render "$APP_DIR/deploy/nginx.conf" > "/etc/nginx/sites-available/$APP_NAME"
    ln -sf "/etc/nginx/sites-available/$APP_NAME" "/etc/nginx/sites-enabled/$APP_NAME"
    if nginx -t 2>/dev/null; then
        systemctl reload nginx
    else
        rm -f "/etc/nginx/sites-enabled/$APP_NAME"
        warn "Конфиг nginx отвергнут, блок отключён. Панель доступна напрямую на 127.0.0.1:$WEB_PORT"
    fi
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "$NGINX_PORT/tcp" >/dev/null 2>&1 || true
    fi
else
    log "8/8 Nginx пропущен (SETUP_NGINX=0)"
fi

# ---------------------------------------------------------------------
IP="$(hostname -I | awk '{print $1}')"
cat <<BANNER

=====================================================================
  Установка завершена. Ничего из уже работавшего на сервере не задето.
=====================================================================

  Код          $APP_DIR
  Данные       $DATA_DIR
  Сервисы      $APP_NAME-bot  $APP_NAME-web  $APP_NAME-worker
  CLI          $APP_NAME-cli
  Панель       http://$IP:$NGINX_PORT/

Сервисы НЕ запущены намеренно: без ключей бот работать не может.

ДАЛЬШЕ — 4 ШАГА:

 1. Впишите ключи:
        nano $APP_DIR/.env
    Обязательно: BOT_TOKEN, OWNER_IDS, TG_API_ID, TG_API_HASH

 2. Авторизуйте торговый аккаунт Telegram (спросит код из Telegram):
        $APP_NAME-cli login

 3. Проверьте конфигурацию:
        $APP_NAME-cli doctor

 4. Запустите:
        systemctl enable --now $APP_NAME-bot $APP_NAME-web $APP_NAME-worker
        systemctl status $APP_NAME-bot --no-pager
BANNER

if [[ "$NEW_ENV" == "1" ]]; then
cat <<CREDS

Панель: логин admin, пароль $(cat "$DATA_DIR/.webpass")
CREDS
fi

cat <<TAIL

Логи:
    journalctl -u $APP_NAME-bot -f
    journalctl -u $APP_NAME-worker -f

Режим по умолчанию — SAFE: бот только показывает находки и ничего
не покупает, пока вы сами не переключите режим.
TAIL
