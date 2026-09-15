"""Сколько людей пользуется ботом — строкой на стартовом экране.

Число рядом с названием работает как довод «сюда уже пришли», но только в одну
сторону: пока пользователей мало, та же строка отговаривает сильнее, чем
убеждает. Поэтому показывать её или нет решает владелец командой /counter, а по
умолчанию её нет — включить проще, чем объясняться за раннее число.

Модуль намеренно не знает ни про базу, ни про Telegram: правило показа и
склонение слова проверяются тестами и читаются целиком.
"""

from __future__ import annotations

from dataclasses import dataclass

STATE_KEY = "user_counter"        # где решение команды /counter лежит в базе


def plural(count: int) -> str:
    """«1 пользователь», «2 пользователя», «5 пользователей»."""
    if 11 <= count % 100 <= 14:
        return "пользователей"
    last = count % 10
    if last == 1:
        return "пользователь"
    if last in {2, 3, 4}:
        return "пользователя"
    return "пользователей"


def fmt_count(count: int) -> str:
    """Разряды пробелом: 5 400 читается, 5400 — нет."""
    return f"{count:,}".replace(",", " ")


@dataclass
class AudienceCounter:
    """Показывать ли число пользователей. Объект общий: /counter меняет его на ходу."""

    enabled: bool = False

    def line(self, count: int) -> str:
        """Готовая строка для экрана либо пустая, если показывать нечего."""
        if not self.enabled or count < 1:
            return ""
        return f"👥 {fmt_count(count)} {plural(count)}"

    def to_state(self) -> str:
        return "1" if self.enabled else "0"

    def apply_state(self, raw: str) -> None:
        """Накладывает сохранённое решение. Мусор в базе ничего не меняет."""
        if raw in {"0", "1"}:
            self.enabled = raw == "1"
