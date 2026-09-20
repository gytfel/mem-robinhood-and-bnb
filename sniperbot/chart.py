"""Маленький график цены прямо в тексте карточки позиции.

Картинкой это было бы хуже. Telegram показал бы её отдельным сообщением на
пол-экрана, каждое обновление стоило бы новой отправки, а вопрос у владельца
позиции короткий: растёт или падает и давно ли. На него отвечает строка из
восьми уровней высоты — она помещается в ту же карточку, обновляется вместе с
ней и не требует ни лишнего запроса, ни библиотек рисования.

Хранится график в самой позиции строкой «секунда:цена». Точек всегда около
тридцати, а шаг между ними растёт вместе с возрастом позиции: у только что
купленного токена это полминуты, у прожившего два часа — четыре минуты. Так
график всегда показывает всю жизнь позиции, а не последние четверть часа.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

LEVELS = "▁▂▃▄▅▆▇█"
MAX_POINTS = 32
MIN_STEP_SECONDS = 30.0


def step_for(seconds: float) -> float:
    """Через сколько секунд имеет смысл записывать следующую точку."""
    return max(MIN_STEP_SECONDS, seconds / MAX_POINTS)


def points(track: str) -> list[tuple[int, Decimal]]:
    """Разбирает хранимую строку. Мусор молча пропускаем: график не то, ради
    чего стоит ронять карточку позиции."""
    found: list[tuple[int, Decimal]] = []
    for chunk in (track or "").split(","):
        moment, _, price = chunk.partition(":")
        try:
            found.append((int(moment), Decimal(price)))
        except (ValueError, ArithmeticError, InvalidOperation):
            continue
    return found


def pack(rows: list[tuple[int, Decimal]]) -> str:
    return ",".join(f"{moment}:{price:.6g}" for moment, price in rows)


def add_point(track: str, seconds: float, price: Decimal | None) -> str:
    """Дописывает замер, если с прошлого прошло достаточно времени."""
    if price is None or price <= 0:
        return track
    moment = max(0, int(seconds))
    rows = points(track)
    if rows and moment - rows[-1][0] < step_for(moment):
        return track
    rows.append((moment, Decimal(price)))
    if len(rows) > MAX_POINTS:
        # Прореживаем через одну: окно растягивается вдвое, а размер строки
        # остаётся прежним. Иначе позиция, прожившая сутки, хранила бы тысячи
        # замеров ради картинки в двадцать символов.
        rows = rows[::2]
    return pack(rows)


def sparkline(values: list[Decimal]) -> str:
    """Ряд цен → строка из блоков. Высота — доля от размаха, не от нуля."""
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        return LEVELS[len(LEVELS) // 2] * len(values)
    return "".join(
        LEVELS[min(len(LEVELS) - 1, int((value - low) / span * len(LEVELS)))]
        for value in values
    )


def render(track: str) -> str:
    """Строка для карточки. Пусто — если рисовать ещё нечего."""
    rows = points(track)
    line = sparkline([price for _, price in rows])
    if not line:
        return ""
    minutes = round((rows[-1][0] - rows[0][0]) / 60)
    span = f"{minutes} мин" if minutes else "меньше минуты"
    # Моноширинный шрифт: иначе блоки разной ширины и график кривой.
    return f"📈 За {span}: <code>{line}</code>"
