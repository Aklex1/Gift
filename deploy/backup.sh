#!/usr/bin/env bash
# Резервная копия бота: база, .env, сессии Telegram.
#
# Копия делается в один каталог-архив с меткой времени и проверяется
# сразу после создания: непроверенный бэкап — это не бэкап, а надежда.
#
# Запуск вручную:
#     APP_DIR=/opt/gift bash deploy/backup.sh
# Запуск по расписанию ставит install.sh (таймер gift-backup.timer).
#
# Восстановление: deploy/restore.sh <каталог-копии>

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/gift}"
DATA_DIR="${DATA_DIR:-/var/lib/gift}"
BACKUP_DIR="${BACKUP_DIR:-$DATA_DIR/backups}"
KEEP="${KEEP:-14}"            # сколько копий хранить
ENV_FILE="$APP_DIR/.env"

log()  { echo -e "\033[1;34m[backup]\033[0m $*"; }
warn() { echo -e "\033[1;33m[backup]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[backup]\033[0m $*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "Не найден $ENV_FILE — укажите APP_DIR=..."

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# Имя должно быть новым. Два запуска в одну секунду иначе попали бы в
# один каталог, и сбой второго снёс бы удачную копию первого.
STAMP="$(date -u +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/$STAMP"
SUFFIX=1
while [[ -e "$DEST" ]]; do
    DEST="$BACKUP_DIR/$STAMP-$SUFFIX"
    SUFFIX=$((SUFFIX + 1))
done
mkdir "$DEST"
chmod 700 "$DEST"

# Оборванная копия опаснее отсутствующей: она выглядит свежей, а данных
# в ней нет. Поэтому незавершённый каталог удаляется при любом выходе,
# и только успешный финал снимает ловушку. Удаляем ровно тот каталог,
# который создали сами.
cleanup() { rm -rf "$DEST" && warn "Неполная копия удалена: $DEST"; }
trap cleanup EXIT

# --- .env целиком: в нём ключ шифрования, без него база бесполезна ---
cp "$ENV_FILE" "$DEST/env"
chmod 600 "$DEST/env"

# --- База ---------------------------------------------------------------
DB_URL="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
[[ -n "$DB_URL" ]] || die "В .env нет DATABASE_URL"

if [[ "$DB_URL" == sqlite* ]]; then
    SQLITE_PATH="${DB_URL#sqlite:///}"
    [[ -f "$SQLITE_PATH" ]] || die "Файл БД не найден: $SQLITE_PATH"
    # Копируем через backup API, а не cp: при активных записях
    # обычная копия файла может оказаться не согласованной. Утилиты
    # sqlite3 на сервере может не быть — берём python из venv, он есть
    # всегда, раз работает сам бот.
    "$APP_DIR/.venv/bin/python" - "$SQLITE_PATH" "$DEST/db.sqlite" <<'PY' \
        || die "Не удалось снять копию SQLite"
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
    a.backup(b)
PY
    gzip -f "$DEST/db.sqlite"
else
    # pg_dump понимает только postgresql://, без имени драйвера.
    PG_URL="${DB_URL/postgresql+psycopg:\/\//postgresql://}"
    pg_dump --no-owner --format=custom --file="$DEST/db.dump" "$PG_URL" \
        || die "pg_dump не смог снять копию базы"
fi

# --- Сессии Telegram ----------------------------------------------------
# Без них после восстановления придётся заново проходить вход по SMS.
if compgen -G "$DATA_DIR"/*.session > /dev/null; then
    mkdir -p "$DEST/sessions"
    cp "$DATA_DIR"/*.session "$DEST/sessions/" 2>/dev/null || true
    chmod 600 "$DEST"/sessions/* 2>/dev/null || true
fi

# --- Проверка копии -----------------------------------------------------
# Битый архив должен обнаруживаться здесь, а не в день аварии.
log "Проверка копии"
if [[ -f "$DEST/db.dump" ]]; then
    pg_restore --list "$DEST/db.dump" > /dev/null \
        || die "Копия базы не читается — бэкап негоден"
    ROWS="$(pg_restore --list "$DEST/db.dump" | grep -c 'TABLE DATA' || true)"
    [[ "$ROWS" -gt 0 ]] || die "В копии нет ни одной таблицы с данными"
elif [[ -f "$DEST/db.sqlite.gz" ]]; then
    gzip -t "$DEST/db.sqlite.gz" || die "Архив базы повреждён"
fi
grep -q '^GIFT_SECRET_KEY=' "$DEST/env" \
    || warn "В копии .env нет GIFT_SECRET_KEY — секреты будет нечем расшифровать"

# --- Опись --------------------------------------------------------------
{
    echo "created_utc=$STAMP"
    echo "app_dir=$APP_DIR"
    echo "data_dir=$DATA_DIR"
    echo "db_kind=$([[ -f "$DEST/db.dump" ]] && echo postgres || echo sqlite)"
    echo "sessions=$(ls "$DEST/sessions" 2>/dev/null | wc -l)"
} > "$DEST/manifest"

( cd "$DEST" && sha256sum -- * sessions/* 2>/dev/null > SHA256SUMS ) || true

trap - EXIT
SIZE="$(du -sh "$DEST" | cut -f1)"
log "Готово: $DEST ($SIZE)"

# --- Чистка старых ------------------------------------------------------
mapfile -t OLD < <(ls -1d "$BACKUP_DIR"/*/ 2>/dev/null | sort | head -n -"$KEEP")
for dir in "${OLD[@]:-}"; do
    [[ -n "$dir" ]] && rm -rf "$dir" && log "Удалена старая копия: $(basename "$dir")"
done

cat <<'NOTE'

ВАЖНО: копия лежит на том же сервере, что и оригинал. От потери сервера
она не спасает. Забирайте её к себе, например:

    scp -r root@СЕРВЕР:ПУТЬ ./gift-backup/

NOTE
echo "    Путь копии: $DEST"
