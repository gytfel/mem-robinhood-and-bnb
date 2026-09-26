"""Кеши с потолком.

Бот работает неделями, а токены и пулы на мемкоин-сети рождаются сотнями в
час. Кеш «по токену» без предела через неделю держит десятки тысяч записей,
из которых нужны последние несколько сотен, — и растит память процесса на
сервере, где её и так в обрез.

Словари Python помнят порядок вставки, поэтому «самые старые» — это просто
первые ключи. Обновлённая запись переезжает в конец: иначе живой, нужный
прямо сейчас токен вылетел бы первым только потому, что его увидели давно.
"""

from __future__ import annotations

from itertools import islice
from typing import TypeVar

K = TypeVar("K")
V = TypeVar("V")


def remember(cache: dict[K, V], key: K, value: V, limit: int) -> None:
    """Кладёт запись в конец и выбрасывает самые старые сверх предела."""
    cache.pop(key, None)
    cache[key] = value
    overflow = len(cache) - limit
    if overflow > 0:
        for stale in list(islice(cache, overflow)):
            del cache[stale]
