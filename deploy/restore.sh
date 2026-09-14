#!/usr/bin/env bash
# Восстановление бота из копии, сделанной deploy/backup.sh.
#
#     APP_DIR=/opt/gift bash deploy/restore.sh /var/lib/gift/backups/20260914-101500
#
# Сервисы останавливаются на время восстановления и НЕ поднимаются
# автоматически: сначала убедитесь, что данные на месте.

set -euo pipefail

SRC="${1:-}"
APP_DIR="${APP_DIR:-/opt/gift}"
DATA_DIR="${DATA_DIR:-/var/lib/gift}"
APP_NAME="${APP_NAME:-gift}"
APP_USER="${APP_USER:-gift}"

log()  { echo -e "\033[1;34m[restore]\033[0m $*"; }
warn() { echo -e "\033[1;33m[restore]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[restore]\033[0m $*" >&2; exit 1; }

[[ -n "$SRC" ]]        || die "Укажите каталог копии: bash deploy/restore.sh /путь/к/20260914-101500"
[[ -d "$SRC" ]]        || die "Каталог не найден: $SRC"
[[ -f "$SRC/env" ]]    || die "В копии нет файла env — это не копия бота"

log "Копия: $SRC"
cat "$SRC/manifest" 2>/dev/null || true

read -rp "Текущие данные будут заменены. Продолжить? [y/N] " ANSWER
[[ "$ANSWER" == "y" || "$ANSWER" == "Y" ]] || die "Отменено"

log "Останавливаю сервисы"
systemctl stop "$APP_NAME-bot" "$APP_NAME-web" "$APP_NAME-worker" 2>/dev/null || true

# --- .env ---------------------------------------------------------------
# Сохраняем текущий: если в копии старый ключ шифрования, понадобится
# сравнить, а безвозвратно затирать рабочий конфиг нельзя.
if [[ -f "$APP_DIR/.env" ]]; then
    cp "$APP_DIR/.env" "$APP_DIR/.env.before-restore"
    log "Прежний .env сохранён как .env.before-restore"
fi
cp "$SRC/env" "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env" 2>/dev/null || true
chmod 600 "$APP_DIR/.env"

# --- База ---------------------------------------------------------------
DB_URL="$(grep -E '^DATABASE_URL=' "$APP_DIR/.env" | head -1 | cut -d= -f2-)"
[[ -n "$DB_URL" ]] || die "В восстановленном .env нет DATABASE_URL"

if [[ -f "$SRC/db.dump" ]]; then
    PG_URL="${DB_URL/postgresql+psycopg:\/\//postgresql://}"
    log "Восстанавливаю PostgreSQL"
    pg_restore --clean --if-exists --no-owner --dbname="$PG_URL" "$SRC/db.dump" \
        || warn "pg_restore завершился с предупреждениями — проверьте вывод выше"
elif [[ -f "$SRC/db.sqlite.gz" ]]; then
    SQLITE_PATH="${DB_URL#sqlite:///}"
    log "Восстанавливаю SQLite в $SQLITE_PATH"
    gzip -dc "$SRC/db.sqlite.gz" > "$SQLITE_PATH"
    chown "$APP_USER:$APP_USER" "$SQLITE_PATH" 2>/dev/null || true
else
    die "В копии нет файла базы"
fi

# --- Сессии -------------------------------------------------------------
if [[ -d "$SRC/sessions" ]]; then
    mkdir -p "$DATA_DIR"
    cp "$SRC/sessions"/*.session "$DATA_DIR/" 2>/dev/null || true
    chown "$APP_USER:$APP_USER" "$DATA_DIR"/*.session 2>/dev/null || true
    chmod 600 "$DATA_DIR"/*.session 2>/dev/null || true
    log "Сессии Telegram восстановлены"
fi

# --- Главная проверка ---------------------------------------------------
# Ключ из копии обязан расшифровывать секреты из копии. Если нет —
# восстановление формально прошло, а торговать бот не сможет.
log "Проверяю, что ключ шифрования подходит к данным"
if sudo -u "$APP_USER" env -C "$APP_DIR" "$APP_DIR/.venv/bin/python" -m app.cli verify-key; then
    log "Данные и ключ согласованы"
else
    warn "Секреты не читаются ключом из копии."
    warn "Нужен тот GIFT_SECRET_KEY, который действовал на момент копии."
    warn "Впишите его в $APP_DIR/.env и повторите: $APP_NAME-cli verify-key"
fi

cat <<NOTE

Восстановление завершено. Сервисы намеренно не запущены.

Проверьте и запустите:
    $APP_NAME-cli doctor
    systemctl start $APP_NAME-bot $APP_NAME-web $APP_NAME-worker

Режим торговли после восстановления проверьте отдельно — он хранится
в базе и вернулся таким, каким был на момент копии.
NOTE
