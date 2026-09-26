"""Сколько памяти занимает бот и сколько осталось на сервере.

Бота уже убивала система за нехватку памяти, и видно это было только из
журнала сервера. Строка в /health показывает то же самое из Telegram: если
память бота растёт день ото дня, это утечка, а если ровная, а свободного на
сервере всё меньше — память ест кто-то другой.
"""

from __future__ import annotations

from pathlib import Path

# Меньше этой доли свободной памяти — сервер на грани: следующий всплеск,
# и система начнёт убивать процессы. Без подкачки запаса нет совсем.
LOW_SHARE = 0.10


def _kilobytes(text: str, field: str) -> int | None:
    """Значение поля из /proc в килобайтах: «VmRSS:   251000 kB»."""
    for line in text.splitlines():
        if line.startswith(field + ":"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def process_mb(status: str) -> int | None:
    """Сколько бот держит в памяти сейчас, из /proc/self/status."""
    rss = _kilobytes(status, "VmRSS")
    return None if rss is None else rss // 1024


def server_mb(meminfo: str) -> tuple[int, int, int] | None:
    """(доступно, всего, подкачка) в мегабайтах, из /proc/meminfo."""
    total = _kilobytes(meminfo, "MemTotal")
    available = _kilobytes(meminfo, "MemAvailable")
    if total is None or available is None:
        return None
    swap = _kilobytes(meminfo, "SwapTotal") or 0
    return available // 1024, total // 1024, swap // 1024


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""     # не Linux или нет доступа — строку просто не покажем


def health_line(status: str | None = None, meminfo: str | None = None) -> str:
    """Строка для /health. Пусто — если система не отдала сведений."""
    own = process_mb(_read("/proc/self/status") if status is None else status)
    server = server_mb(_read("/proc/meminfo") if meminfo is None else meminfo)
    parts = []
    if own is not None:
        parts.append(f"бот {own} МБ")
    warning = ""
    if server is not None:
        available, total, swap = server
        parts.append(f"свободно на сервере {available} из {total} МБ")
        if not swap:
            parts.append("подкачки нет")
        if total and available / total < LOW_SHARE:
            warning = "⚠️ "
    if not parts:
        return ""
    return f"{warning}Память: " + " · ".join(parts)
