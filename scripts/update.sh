#!/usr/bin/env bash
# Обновление установленного бота: свежий код -> зависимости -> перезапуск сервиса.
#
#   cd <клон репозитория> && sudo bash scripts/update.sh
#
# Скрипт запускается из клона репозитория (там, где лежит .git), а не из каталога
# установки: в /opt копия кода без истории git, обновлять её нечем.
#
# Переменные окружения (необязательно):
#   APP_DIR=/opt/memecoin-sniper   куда установлен бот (по умолчанию берётся из systemd)
#   APP_USER=sniper                от чьего имени работает сервис
#   SERVICE_NAME=memecoin-sniper   имя systemd-сервиса
#   NO_PULL=1                      не делать git pull, обновить тем, что уже в клоне
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-memecoin-sniper}"
APP_USER="${APP_USER:-sniper}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "запустите через sudo: sudo bash scripts/update.sh"

# --- где установлен бот ------------------------------------------------------
# Каталог установки берём из systemd: так обновление попадает туда, откуда
# сервис действительно запускается, даже если при установке задавали свой путь.
detect_app_dir() {
    local from_unit
    from_unit="$(systemctl show "$SERVICE_NAME" -p WorkingDirectory --value 2>/dev/null || true)"
    from_unit="${from_unit#-}"          # systemd помечает необязательный путь дефисом
    if [ -n "$from_unit" ] && [ -d "$from_unit" ]; then
        echo "$from_unit"
        return
    fi
    echo "/opt/memecoin-sniper"
}

APP_DIR="${APP_DIR:-$(detect_app_dir)}"

[ -d "$APP_DIR" ] || fail "каталог установки $APP_DIR не найден.
Укажите свой:  sudo APP_DIR=/путь/к/боту bash scripts/update.sh
Посмотреть, откуда работает сервис:  systemctl cat $SERVICE_NAME | grep WorkingDirectory"
[ -x "$APP_DIR/.venv/bin/pip" ] || fail "в $APP_DIR нет окружения .venv — похоже, бот сюда не устанавливали.
Первая установка делается так:  sudo bash scripts/install-server.sh"

# --- свежий код --------------------------------------------------------------
if [ -z "${NO_PULL:-}" ] && [ -d "$SRC_DIR/.git" ]; then
    branch="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    say "Забираю свежий код ветки $branch"
    git -C "$SRC_DIR" fetch --quiet origin "$branch" || fail "не смог связаться с GitHub"
    git -C "$SRC_DIR" merge --ff-only "origin/$branch" \
        || fail "в клоне есть свои изменения — обновление остановлено.
Отбросить их и обновиться:  git -C $SRC_DIR reset --hard origin/$branch"
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

say "Резервная копия базы"
APP_DIR="$APP_DIR" bash "$SRC_DIR/scripts/backup.sh" || true

if [ "$SRC_DIR" != "$APP_DIR" ]; then
    say "Копирую новый код в $APP_DIR"
    # .env и data не трогаем: там ключи и кошельки.
    tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=data --exclude=.env \
        --exclude=__pycache__ --exclude='*.pyc' -cf - . | tar -C "$APP_DIR" -xf -
elif [ -d "$APP_DIR/.git" ]; then
    say "Клон и установка — один каталог, код уже обновлён"
fi

say "Обновляю зависимости"
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"

# Проверяем, откуда бот на самом деле возьмёт код. Если пакет когда-то ставили
# обычной установкой, python импортирует копию из site-packages, и новый код в
# APP_DIR так и остаётся невостребованным — обновление «не доходит».
code_root() {
    "$APP_DIR/.venv/bin/python" -c \
        'import sniperbot, os; print(os.path.dirname(os.path.dirname(sniperbot.__file__)))' 2>/dev/null || true
}
if [ "$(code_root)" != "$APP_DIR" ]; then
    say "Код брался из $(code_root) — переставляю пакет на $APP_DIR"
    "$APP_DIR/.venv/bin/pip" uninstall -y -q memecoin-sniper-bot >/dev/null 2>&1 || true
    "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
    [ "$(code_root)" = "$APP_DIR" ] || fail "python всё ещё берёт код из $(code_root).
Проще всего пересоздать окружение:
  rm -rf $APP_DIR/.venv && python3 -m venv $APP_DIR/.venv \\
    && $APP_DIR/.venv/bin/pip install -e $APP_DIR"
fi

# Свежий ли код доехал: файла перехвата разгона нет в старых сборках.
[ -f "$APP_DIR/sniperbot/sniper/hunter.py" ] \
    || fail "в $APP_DIR не оказалось нового кода — проверьте, из какой ветки клон:
  git -C $SRC_DIR rev-parse --abbrev-ref HEAD"
if id -u "$APP_USER" >/dev/null 2>&1; then
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi

systemctl list-unit-files "$SERVICE_NAME.service" --no-legend 2>/dev/null | grep -q . \
    || fail "systemd-сервиса $SERVICE_NAME нет.
Если он называется иначе:  sudo SERVICE_NAME=имя bash scripts/update.sh
Посмотреть список:  systemctl list-units --type=service | grep -i snip"

say "Перезапускаю сервис $SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true

commit="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD 2>/dev/null || echo '?')"
printf '\n\033[1mГотово. Установлена сборка %s в %s\033[0m\n' "$commit" "$APP_DIR"
printf 'Проверить в боте: /version · логи: journalctl -u %s -f\n' "$SERVICE_NAME"
