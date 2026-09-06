#!/usr/bin/env bash
# Обновление установленного бота: код -> зависимости -> перезапуск сервиса.
#   sudo bash scripts/update.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/memecoin-sniper}"
APP_USER="${APP_USER:-sniper}"
SERVICE_NAME="memecoin-sniper"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "запустите через sudo" >&2; exit 1; }

echo "==> Резервная копия базы"
bash "$SRC_DIR/scripts/backup.sh" || true

if [ "$SRC_DIR" != "$APP_DIR" ]; then
    echo "==> Копирую новый код в $APP_DIR"
    tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=data --exclude=.env \
        --exclude=__pycache__ --exclude='*.pyc' -cf - . | tar -C "$APP_DIR" -xf -
elif [ -d "$APP_DIR/.git" ]; then
    echo "==> git pull"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
fi

echo "==> Обновляю зависимости"
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Перезапускаю сервис"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true
