#!/usr/bin/env bash
# Установка Memecoin Sniper Bot на сервер (Ubuntu/Debian) как systemd-сервис.
#
#   sudo bash scripts/install-server.sh
#
# Переменные окружения (необязательно):
#   APP_DIR=/opt/memecoin-sniper   куда установить
#   APP_USER=sniper                от чьего имени работает сервис
#   BOT_TOKEN=...  ADMIN_IDS=...   чтобы не отвечать на вопросы вручную
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/memecoin-sniper}"
APP_USER="${APP_USER:-sniper}"
SERVICE_NAME="${SERVICE_NAME:-memecoin-sniper}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "запустите через sudo: sudo bash scripts/install-server.sh"

say "Устанавливаю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates >/dev/null

if ! id -u "$APP_USER" >/dev/null 2>&1; then
    say "Создаю системного пользователя $APP_USER"
    useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
fi


write_build_stamp() {
    # Отпечаток сборки: в /opt каталога .git нет, поэтому версию фиксируем здесь.
    local commit date branch
    commit="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD 2>/dev/null || true)"
    [ -n "$commit" ] || return 0
    date="$(git -C "$SRC_DIR" log -1 --format=%cd --date=format:'%d.%m %H:%M' 2>/dev/null || true)"
    branch="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    printf '{"version":"1.0.0","commit":"%s","date":"%s","branch":"%s"}\n' \
        "$commit" "$date" "$branch" > "$SRC_DIR/BUILD"
}

write_build_stamp

say "Копирую код в $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
    tar -C "$SRC_DIR" \
        --exclude=.git --exclude=.venv --exclude=data --exclude=.env \
        --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache \
        -cf - . | tar -C "$APP_DIR" -xf -
fi
mkdir -p "$APP_DIR/data"

say "Ставлю зависимости в $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
    say "Создаю $APP_DIR/.env"
    if [ -n "${BOT_TOKEN:-}" ]; then
        "$APP_DIR/.venv/bin/sniper" --env-file "$APP_DIR/.env" init --yes \
            --bot-token "$BOT_TOKEN" ${ADMIN_IDS:+--admin-id "$ADMIN_IDS"} ${BSC_RPC_URLS:+--bsc-rpc "$BSC_RPC_URLS"}
    else
        "$APP_DIR/.venv/bin/sniper" --env-file "$APP_DIR/.env" init
    fi
else
    say ".env уже есть — оставляю как есть"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

say "Ставлю systemd-сервис $SERVICE_NAME"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
    "$APP_DIR/deploy/memecoin-sniper.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

say "Проверяю конфигурацию"
sudo -u "$APP_USER" env HOME="/home/$APP_USER" \
    sh -c "cd '$APP_DIR' && '$APP_DIR/.venv/bin/sniper' --env-file '$APP_DIR/.env' doctor" || \
    printf '\033[33mДиагностика нашла проблемы — исправьте их и запустите:\n  sudo -u %s %s/.venv/bin/sniper --env-file %s/.env doctor\033[0m\n' \
        "$APP_USER" "$APP_DIR" "$APP_DIR"

say "Запускаю сервис"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager --lines=15 status "$SERVICE_NAME" || true

cat <<INFO

Готово. Полезные команды:

  systemctl status $SERVICE_NAME       состояние
  journalctl -u $SERVICE_NAME -f       логи в реальном времени
  systemctl restart $SERVICE_NAME      перезапуск
  systemctl stop $SERVICE_NAME         остановить

  cd $APP_DIR && sudo -u $APP_USER .venv/bin/sniper --env-file .env doctor
  cd $APP_DIR && sudo -u $APP_USER .venv/bin/sniper --env-file .env wallets

Файл с ключами: $APP_DIR/.env   (сделайте резервную копию MASTER_KEY!)
База кошельков: $APP_DIR/data/sniper.db
INFO
