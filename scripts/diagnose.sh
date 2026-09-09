#!/usr/bin/env bash
# Диагностика установки: какой код бота на самом деле выполняется прямо сейчас.
#
#   sudo bash scripts/diagnose.sh
#
# Отвечает на вопрос «почему обновление не дошло»: находит работающий процесс,
# показывает каталог, откуда он импортирует код, и говорит, свежий этот код или нет.
# Ничего не меняет — только смотрит.

# Без set -e: диагностика должна доработать до конца, даже если часть проверок падает.
set -uo pipefail

MARKER="sniperbot/sniper/hunter.py"   # файл, которого нет в старых сборках

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

fresh() { [ -f "$1/$MARKER" ]; }

describe_dir() {
    # Что лежит в каталоге и свежий ли там код.
    local dir="$1"
    [ -d "$dir" ] || return 1
    if fresh "$dir"; then
        ok "$dir — код свежий (есть перехват разгона)"
    else
        bad "$dir — код старый (нет $MARKER)"
    fi
    [ -f "$dir/BUILD" ] && info "BUILD: $(tr -d '\n' < "$dir/BUILD")"
    [ -d "$dir/.git" ] && info "git: $(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null) @ $(git -C "$dir" rev-parse --short=8 HEAD 2>/dev/null)"
    [ -f "$dir/.env" ] && info ".env на месте"
    [ -f "$dir/data/sniper.db" ] && info "база: $dir/data/sniper.db"
    return 0
}

say "1. Работающие процессы бота"
pids="$(pgrep -f 'sniper run|sniperbot' 2>/dev/null | tr '\n' ' ')"
if [ -z "${pids// /}" ]; then
    bad "процесс бота не найден — бот не запущен"
else
    for pid in $pids; do
        exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
        cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
        started="$(ps -o lstart= -p "$pid" 2>/dev/null | xargs || true)"
        printf '  PID %s · запущен %s\n' "$pid" "${started:-?}"
        info "рабочий каталог: ${cwd:-?}"
        info "python: ${exe:-?}"
        # Откуда процесс берёт код — это и есть настоящий ответ.
        if [ -n "$exe" ]; then
            # Из корня: иначе python подставит текущий каталог и покажет его код.
            pkg="$( (cd / && "$exe" -c 'import sniperbot,os;print(os.path.dirname(os.path.dirname(sniperbot.__file__)))' 2>/dev/null) || true)"
            if [ -n "$pkg" ]; then
                info "код импортируется из: $pkg"
                if fresh "$pkg"; then
                    ok "это свежая сборка"
                else
                    bad "это СТАРАЯ сборка — обновлять нужно именно $pkg"
                fi
            fi
        fi
    done
fi

say "2. systemd"
unit="$(systemctl list-unit-files --no-legend 2>/dev/null | awk '/sniper|memecoin/{print $1}' | head -5)"
if [ -z "$unit" ]; then
    bad "сервисов с именем sniper/memecoin нет"
else
    for name in $unit; do
        state="$(systemctl is-active "$name" 2>/dev/null)"
        wd="$(systemctl show "$name" -p WorkingDirectory --value 2>/dev/null)"
        ex="$(systemctl show "$name" -p ExecStart --value 2>/dev/null | head -c 160)"
        printf '  %s · %s\n' "$name" "$state"
        info "WorkingDirectory: ${wd:-—}"
        info "ExecStart: ${ex:-—}"
    done
fi

say "3. Docker"
if command -v docker >/dev/null 2>&1; then
    out="$(docker ps -a --filter name=sniper --format '  {{.Names}} · {{.Status}} · образ {{.Image}}' 2>/dev/null)"
    if [ -n "$out" ]; then
        printf '%s\n' "$out"
        info "если бот работает в Docker, обновление делается так:"
        info "cd <каталог с docker-compose.yml> && git pull && docker compose up -d --build"
    else
        info "контейнеров бота нет"
    fi
else
    info "docker не установлен"
fi

say "4. Каталоги, похожие на установку бота"
found=0
for dir in /opt/* /root/* /home/*/* /srv/*; do
    [ -d "$dir/sniperbot" ] || continue
    describe_dir "$dir" && found=1
done
[ "$found" -eq 1 ] || bad "каталогов с кодом бота не найдено"

say "Что дальше"
cat <<'HINT'
  Обновлять нужно тот каталог, который в пункте 1 помечен как «код импортируется из».
  Из клона репозитория:
      cd <клон> && sudo APP_DIR=<этот каталог> bash scripts/update.sh
  Клон потерялся — склонируйте заново, установка не пострадает:
      git clone -b claude/memecoin-sniper-bot-1nkbtm \
          https://github.com/gytfel/mem-robinhood-and-bnb.git ~/mem-robinhood-and-bnb
HINT
