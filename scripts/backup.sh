#!/usr/bin/env bash
# Резервная копия базы с кошельками.
#   bash scripts/backup.sh [каталог]
#
# ВАЖНО: база бесполезна без MASTER_KEY из .env — храните их вместе,
# но не в одном открытом месте.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/memecoin-sniper}"
DB_PATH="${DB_PATH:-$APP_DIR/data/sniper.db}"
DEST="${1:-$APP_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"

[ -f "$DB_PATH" ] || { echo "База $DB_PATH не найдена — нечего копировать"; exit 0; }

mkdir -p "$DEST"
if command -v sqlite3 >/dev/null 2>&1; then
    # безопасно копирует базу даже во время работы бота
    sqlite3 "$DB_PATH" ".backup '$DEST/sniper-$STAMP.db'"
else
    cp "$DB_PATH" "$DEST/sniper-$STAMP.db"
fi
chmod 600 "$DEST/sniper-$STAMP.db"

# оставляем 14 последних копий
ls -1t "$DEST"/sniper-*.db 2>/dev/null | tail -n +15 | xargs -r rm --

echo "Копия: $DEST/sniper-$STAMP.db"
echo "Не забудьте сохранить MASTER_KEY из $APP_DIR/.env — без него копия бесполезна."
