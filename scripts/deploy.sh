#!/usr/bin/env bash
# Привести сервер в рабочее состояние: свежий код, запущенная служба, проверка.
#
#   cd <клон репозитория> && sudo bash scripts/deploy.sh
#
# В отличие от update.sh ничего не предполагает о текущей установке. Сам находит,
# где и как бот запущен (systemd, Docker или вручную), останавливает всё лишнее,
# ставит новый код и убеждается, что работает именно он. Кошельки и настройки
# (.env и data/) не трогаются никогда.
set -uo pipefail

SERVICE_NAME="${SERVICE_NAME:-memecoin-sniper}"
APP_USER="${APP_USER:-sniper}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="sniperbot/sniper/hunter.py"     # файла нет в старых сборках

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '\n\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "запустите через sudo: sudo bash scripts/deploy.sh"
# Проверяем systemd до того, как что-то останавливать: иначе на машине без него
# бот окажется выключенным, а поднять его будет нечем.
[ -d /run/systemd/system ] || fail "на этой машине нет systemd.
Скорее всего бот запущен в Docker — тогда обновление делается так:
  cd <каталог с docker-compose.yml> && git pull && docker compose up -d --build
Посмотреть, что и как запущено:  bash scripts/diagnose.sh"

[ -f "$SRC_DIR/$MARKER" ] || fail "в клоне нет нового кода.
Проверьте ветку:  git -C $SRC_DIR rev-parse --abbrev-ref HEAD
Нужна ветка claude/memecoin-sniper-bot-1nkbtm"

code_root_of() {
    # Куда python указанного окружения резолвит пакет бота.
    # Запускаем из корня: python ставит текущий каталог первым в sys.path, и из
    # клона репозитория проверка увидела бы код клона, а не установленный.
    [ -x "${1:-}" ] || return 0
    (cd / && "$1" -c \
        'import sniperbot, os; print(os.path.dirname(os.path.dirname(sniperbot.__file__)))' \
        2>/dev/null) || true
}

# --------------------------------------------------------------- 1. что сейчас
say "1. Ищу текущую установку"

RUNNING_DIR=""
for pid in $(pgrep -f 'sniper run|sniperbot' 2>/dev/null); do
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null)"
    # Интересуют только процессы python: под шаблон попадает и сам этот скрипт,
    # если его запустили по пути с «sniperbot» внутри.
    case "$(basename "${exe:-нет}")" in python*) ;; *) continue ;; esac
    root="$(code_root_of "$exe")"
    [ -n "$root" ] || continue
    ok "работает PID $pid, код из $root"
    RUNNING_DIR="$root"
done
[ -n "$RUNNING_DIR" ] || warn "работающего процесса бота не нашёл"

DOCKER_DIR=""
if command -v docker >/dev/null 2>&1; then
    cid="$(docker ps -aq --filter name=sniper 2>/dev/null | head -1)"
    if [ -n "$cid" ]; then
        DOCKER_DIR="$(docker inspect -f \
            '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$cid" 2>/dev/null)"
        ok "контейнер $(docker inspect -f '{{.Name}}' "$cid" | tr -d /) · каталог ${DOCKER_DIR:-неизвестен}"
    fi
fi

UNIT_DIR="$(systemctl show "$SERVICE_NAME" -p WorkingDirectory --value 2>/dev/null)"
UNIT_DIR="${UNIT_DIR#-}"
[ -n "$UNIT_DIR" ] && ok "сервис $SERVICE_NAME настроен на $UNIT_DIR"

# Каталог установки: тот, откуда реально работает бот; иначе тот, где лежат
# настройки и база; иначе путь из юнита; иначе значение по умолчанию.
pick_app_dir() {
    [ -n "${APP_DIR:-}" ] && { echo "$APP_DIR"; return; }
    [ -n "$RUNNING_DIR" ] && [ -d "$RUNNING_DIR" ] && { echo "$RUNNING_DIR"; return; }
    for dir in "$UNIT_DIR" /opt/memecoin-sniper /opt/sniperbot /root/* /home/*/*; do
        [ -n "$dir" ] && [ -f "$dir/.env" ] && [ -d "$dir/sniperbot" ] && { echo "$dir"; return; }
    done
    echo "${UNIT_DIR:-/opt/memecoin-sniper}"
}
APP_DIR="$(pick_app_dir)"
say "Каталог установки: $APP_DIR"

[ -f "$APP_DIR/.env" ] || fail "в $APP_DIR нет файла .env — бот сюда ещё не устанавливали.
Первая установка (создаст кошелёк и спросит ключи):
  sudo bash scripts/install-server.sh
Установка уже есть в другом месте? Укажите её:
  sudo APP_DIR=/путь bash scripts/deploy.sh"

# ------------------------------------------------------------ 2. остановить всё
say "2. Останавливаю бота"
if [ -n "$DOCKER_DIR" ]; then
    docker ps -q --filter name=sniper | xargs -r docker stop >/dev/null 2>&1 && ok "контейнеры остановлены"
fi
for name in $(systemctl list-unit-files --no-legend 2>/dev/null | awk '/sniper|memecoin/{print $1}'); do
    systemctl stop "$name" >/dev/null 2>&1 && ok "остановлен $name"
done
# Запуски вручную (nohup, screen) systemd не видит, а порт Telegram они занимают.
for pid in $(pgrep -f 'sniper run' 2>/dev/null); do
    kill "$pid" 2>/dev/null && warn "остановлен ручной запуск PID $pid"
done
sleep 2

# ------------------------------------------------------------------ 3. код
say "3. Ставлю новый код в $APP_DIR"
bash "$SRC_DIR/scripts/backup.sh" >/dev/null 2>&1 && ok "база скопирована в $APP_DIR/backups"

commit="$(git -C "$SRC_DIR" rev-parse --short=8 HEAD 2>/dev/null)"
if [ -n "$commit" ]; then
    printf '{"version":"1.0.0","commit":"%s","date":"%s","branch":"%s"}\n' \
        "$commit" \
        "$(git -C "$SRC_DIR" log -1 --format=%cd --date=format:'%d.%m %H:%M' 2>/dev/null)" \
        "$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)" > "$SRC_DIR/BUILD"
fi

if [ "$SRC_DIR" != "$APP_DIR" ]; then
    tar -C "$SRC_DIR" --exclude=.git --exclude=.venv --exclude=data --exclude=.env \
        --exclude=backups --exclude=__pycache__ --exclude='*.pyc' -cf - . \
        | tar -C "$APP_DIR" -xf - || fail "не смог скопировать код в $APP_DIR"
fi
[ -f "$APP_DIR/$MARKER" ] || fail "новый код не доехал в $APP_DIR"
ok "код на месте, сборка $commit"

# --------------------------------------------------------------- 4. окружение
say "4. Проверяю окружение Python"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    warn "окружения нет — создаю"
    python3 -m venv "$APP_DIR/.venv" || fail "не смог создать venv (нужен пакет python3-venv)"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR" || fail "не установились зависимости"

# Обычная (не editable) установка — самая частая причина «обновил, а всё старое»:
# python продолжает брать копию из site-packages.
if [ "$(code_root_of "$APP_DIR/.venv/bin/python")" != "$APP_DIR" ]; then
    warn "python берёт код не из $APP_DIR — пересоздаю окружение"
    rm -rf "$APP_DIR/.venv"
    python3 -m venv "$APP_DIR/.venv" || fail "не смог создать venv"
    "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1
    "$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR" || fail "не установились зависимости"
fi
[ "$(code_root_of "$APP_DIR/.venv/bin/python")" = "$APP_DIR" ] \
    || fail "python всё ещё берёт код из $(code_root_of "$APP_DIR/.venv/bin/python")"
"$APP_DIR/.venv/bin/python" -c 'import sniperbot.sniper.hunter' \
    || fail "новый код не импортируется — покажите вывод этой команды"
ok "python импортирует свежий код из $APP_DIR"

# .env мы не перезаписываем — там ключи. Но новые настройки в нём появиться
# должны, иначе файл молча отстаёт от версии и человек о них не узнает.
added="$(cd "$APP_DIR" && "$APP_DIR/.venv/bin/sniper" --env-file "$APP_DIR/.env" \
    env-sync 2>&1 | grep -c '^   · ' || true)"
if [ "${added:-0}" -gt 0 ]; then
    ok "в .env дописано новых настроек: $added (значения пустые, заполните нужные)"
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env" 2>/dev/null || true
fi

# ------------------------------------------------------------------ 5. служба
say "5. Настраиваю службу $SERVICE_NAME"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "/home/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
    ok "создан пользователь $APP_USER"
fi
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__APP_USER__|$APP_USER|g" \
    "$APP_DIR/deploy/memecoin-sniper.service" > "/etc/systemd/system/$SERVICE_NAME.service"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
systemctl restart "$SERVICE_NAME" || fail "служба не запустилась: journalctl -u $SERVICE_NAME -n 50"
ok "служба перезапущена"

# ------------------------------------------------------------------ 6. проверка
say "6. Проверяю, что работает новый код"
sleep 6
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    printf '\n'
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager
    fail "служба упала — причина в логе выше"
fi

pid="$(systemctl show "$SERVICE_NAME" -p MainPID --value 2>/dev/null)"
root="$(code_root_of "$(readlink -f "/proc/$pid/exe" 2>/dev/null)")"
[ "$root" = "$APP_DIR" ] || fail "процесс работает с кодом из ${root:-неизвестно}, а не из $APP_DIR"
ok "PID $pid, код из $APP_DIR"
grep -q "Перехват разгона запущен" <(journalctl -u "$SERVICE_NAME" -n 80 --no-pager 2>/dev/null) \
    && ok "перехват разгона запущен" \
    || warn "в логе пока нет строки о перехвате разгона — она появится после /on"

printf '\n\033[1m✅ Готово. Работает сборка %s из %s\033[0m\n' "$commit" "$APP_DIR"
cat <<HINT

В Telegram проверьте:
  /version    должна показать коммит $commit без предупреждений
  /trending   рейтинг разгоняющихся токенов
  /preset     готовые наборы настроек

Сообщение о перезапуске придёт автоматически. Если его нет — /set restart on.
Логи: journalctl -u $SERVICE_NAME -f
HINT
