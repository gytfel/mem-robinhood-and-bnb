"""Дописывание новых настроек в существующий .env.

Обновление никогда не перезаписывает `.env` — там ключи и пароли. Из-за этого
файл на сервере тихо отстаёт: в новой версии появилась настройка, а человек о
ней не узнает, пока не сравнит свой файл с примером построчно.

Здесь только недостающие ключи и только в конец файла. Существующие строки не
трогаются вообще — ни значения, ни порядок, ни комментарии: файл с ключами это
последнее место, где уместна самодеятельность.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=")
MARKER = "# --- Добавлено обновлением ---"


@dataclass(slots=True)
class Entry:
    """Настройка из примера вместе с поясняющими её комментариями."""

    key: str
    line: str
    comments: tuple[str, ...] = ()


def keys_of(text: str) -> set[str]:
    """Ключи, заданные в файле. Закомментированные не в счёт — они выключены."""
    found = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = KEY_RE.match(line)
        if match:
            found.add(match.group(1))
    return found


def entries_of(text: str) -> list[Entry]:
    """Разбирает пример: каждая настройка со своим блоком комментариев над ней."""
    entries: list[Entry] = []
    comments: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            comments.clear()          # пустая строка обрывает связь с комментарием
            continue
        if stripped.startswith("#"):
            comments.append(line)
            continue
        match = KEY_RE.match(line)
        if match:
            entries.append(Entry(match.group(1), line, tuple(comments)))
        comments.clear()
    return entries


def missing(example: str, current: str) -> list[Entry]:
    """Настройки, которые есть в примере, но отсутствуют в рабочем файле."""
    have = keys_of(current)
    seen: set[str] = set()
    result = []
    for entry in entries_of(example):
        if entry.key in have or entry.key in seen:
            continue
        seen.add(entry.key)
        result.append(entry)
    return result


def render_block(entries: list[Entry]) -> str:
    """Блок для дописывания в конец .env — с комментариями из примера."""
    if not entries:
        return ""
    parts = ["", MARKER]
    for entry in entries:
        parts.extend(entry.comments)
        parts.append(entry.line)
    return "\n".join(parts) + "\n"


def merge(example: str, current: str) -> tuple[str, list[str]]:
    """Возвращает (новое содержимое, список добавленных ключей).

    Если добавлять нечего, содержимое возвращается неизменным — вплоть до
    последнего символа, чтобы повторный запуск ничего не менял.
    """
    entries = missing(example, current)
    if not entries:
        return current, []
    tail = current if current.endswith("\n") or not current else current + "\n"
    return tail + render_block(entries), [entry.key for entry in entries]
