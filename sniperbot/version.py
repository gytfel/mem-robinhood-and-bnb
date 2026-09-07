"""Сведения о запущенной сборке: версия, коммит, дата.

Источники по убыванию надёжности:

1. переменные окружения ``SNIPER_BUILD_*`` — их выставляет Docker или systemd;
2. файл ``BUILD`` рядом с кодом — его пишут скрипты установки и обновления
   (в ``/opt/memecoin-sniper`` каталога ``.git`` нет, код туда копируется);
3. сам git, если бот запущен прямо из репозитория.

Если ничего не доступно, возвращается «неизвестно» — это не ошибка, просто
сообщение о перезапуске будет чуть менее подробным.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = "1.0.0"
ROOT_DIR = Path(__file__).resolve().parent.parent
BUILD_FILE = ROOT_DIR / "BUILD"
UNKNOWN = "неизвестно"


@dataclass(slots=True)
class BuildInfo:
    version: str = VERSION
    commit: str = UNKNOWN
    date: str = ""
    branch: str = ""
    source: str = "unknown"      # env | file | git | unknown

    @property
    def known(self) -> bool:
        return self.commit != UNKNOWN

    def short(self) -> str:
        parts = [f"v{self.version}"]
        if self.known:
            parts.append(f"коммит {self.commit}")
        if self.date:
            parts.append(self.date)
        return " · ".join(parts)

    def same_code_as(self, other: BuildInfo | str | None) -> bool:
        """Совпадает ли код с прошлым запуском (по коммиту, иначе по версии)."""
        previous = other.commit if isinstance(other, BuildInfo) else other
        if not previous or not self.known or previous == UNKNOWN:
            return False
        return self.commit == previous


def _from_env() -> BuildInfo | None:
    commit = os.getenv("SNIPER_BUILD_COMMIT", "").strip()
    if not commit:
        return None
    return BuildInfo(
        version=os.getenv("SNIPER_BUILD_VERSION", VERSION).strip() or VERSION,
        commit=commit[:12],
        date=os.getenv("SNIPER_BUILD_DATE", "").strip(),
        branch=os.getenv("SNIPER_BUILD_BRANCH", "").strip(),
        source="env",
    )


def _from_file(path: Path = BUILD_FILE) -> BuildInfo | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("Файл сборки %s нечитаем: %s", path, exc)
        return None
    commit = str(data.get("commit", "")).strip()
    if not commit:
        return None
    return BuildInfo(
        version=str(data.get("version", VERSION)) or VERSION,
        commit=commit[:12],
        date=str(data.get("date", "")),
        branch=str(data.get("branch", "")),
        source="file",
    )


def _from_git(root: Path = ROOT_DIR) -> BuildInfo | None:
    if not (root / ".git").exists():
        return None

    def run(*args: str) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - фиксированная команда git
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("git %s: %s", args, exc)
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = run("rev-parse", "--short=8", "HEAD")
    if not commit:
        return None
    return BuildInfo(
        commit=commit,
        date=run("log", "-1", "--format=%cd", "--date=format:%d.%m %H:%M"),
        branch=run("rev-parse", "--abbrev-ref", "HEAD"),
        source="git",
    )


def build_info() -> BuildInfo:
    for source in (_from_env, _from_file, _from_git):
        info = source()
        if info is not None:
            return info
    return BuildInfo()


def write_build_file(info: BuildInfo, path: Path = BUILD_FILE) -> None:
    """Сохраняет отпечаток сборки — вызывается скриптами обновления."""
    payload = {"version": info.version, "commit": info.commit,
               "date": info.date, "branch": info.branch}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
