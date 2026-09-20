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
MAX_POINTS = 240
MIN_STEP_SECONDS = 6.0        # шаг опроса позиций: чаще замеров всё равно нет
CANDLES = 24                  # столько свечей помещается в картинку не тесня


def step_for(seconds: float) -> float:
    """Через сколько секунд писать следующую точку.

    Считаем от того, сколько уже накоплено, а не от возраста позиции. Иначе
    после перезапуска бота позиция, открытая час назад, ждала бы первую пару
    точек минуты — а показывать её надо сразу.
    """
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
    if rows and moment - rows[-1][0] < step_for(moment - rows[0][0]):
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
    # В карточке место на два-три десятка символов, а замеров бывают сотни:
    # берём закрытия свечей, ту же сетку, что и на картинке.
    line = sparkline([bar[3] for bar in candles(track, CANDLES)])
    if not line:
        return ""
    minutes = round((rows[-1][0] - rows[0][0]) / 60)
    span = f"{minutes} мин" if minutes else "меньше минуты"
    # Моноширинный шрифт: иначе блоки разной ширины и график кривой.
    return f"📈 За {span}: <code>{line}</code>"


# ----------------------------------------------------------------- свечи
def candles(track: str, count: int = CANDLES) -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Замеры → свечи (открытие, максимум, минимум, закрытие).

    Открытием каждой свечи берём закрытие предыдущей: цена между замерами
    никуда не прыгала, и рисовать разрыв там, где его не было, — врать.
    """
    rows = points(track)
    if len(rows) < 2:
        return []
    start, finish = rows[0][0], rows[-1][0]
    span = finish - start
    if span <= 0:
        return []
    buckets: list[list[Decimal]] = [[] for _ in range(max(1, count))]
    for moment, price in rows:
        place = min(len(buckets) - 1, int((moment - start) / span * len(buckets)))
        buckets[place].append(price)

    bars: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    previous: Decimal | None = None
    for values in buckets:
        if not values:
            if previous is None:
                continue          # до первого замера рисовать нечего
            values = [previous]   # в промежутке сделок не было: свеча-черта
        opening = previous if previous is not None else values[0]
        closing = values[-1]
        bars.append((opening, max([opening, *values]), min([opening, *values]), closing))
        previous = closing
    return bars


# --------------------------------------------------------------- картинка
WIDTH, HEIGHT = 680, 260
PADDING = 10
BACKGROUND = (19, 23, 34)
GRID = (33, 40, 55)
UP = (38, 166, 154)
DOWN = (239, 83, 80)
ENTRY_LINE = (229, 86, 86)
GRID_LINES = 4


def draw(track: str, entry: Decimal | None = None, count: int = CANDLES) -> bytes | None:
    """Свечной график в PNG. ``None`` — замеров пока слишком мало."""
    from sniperbot.utils.png import Canvas

    bars = candles(track, count)
    if len(bars) < 2:
        return None

    prices = [value for bar in bars for value in bar]
    if entry and entry > 0:
        # Линия входа обязана попасть в кадр: график без неё отвечает на
        # вопрос «как ходила цена», а не на вопрос «я в плюсе или в минусе».
        prices.append(entry)
    low, high = min(prices), max(prices)
    if high <= low:
        high, low = high * Decimal("1.001"), low * Decimal("0.999")
    if high <= low:                       # цена ровно ноль — рисовать нечего
        return None

    canvas = Canvas(WIDTH, HEIGHT, BACKGROUND)
    field = HEIGHT - 2 * PADDING

    def y_of(price: Decimal) -> float:
        return PADDING + float((high - price) / (high - low)) * field

    for step in range(1, GRID_LINES):
        canvas.rect(0, PADDING + field * step / GRID_LINES, WIDTH, 1, GRID)
    if entry and entry > 0:
        canvas.dashes(y_of(entry), ENTRY_LINE)

    slot = (WIDTH - 2 * PADDING) / len(bars)
    body_width = max(3.0, slot * 0.62)
    wick_width = max(1.0, body_width // 4)
    for index, (opening, top, bottom, closing) in enumerate(bars):
        middle = PADDING + slot * index + slot / 2
        color = UP if closing >= opening else DOWN
        canvas.rect(middle - wick_width / 2, y_of(top), wick_width,
                    max(1.0, y_of(bottom) - y_of(top)), color)
        body_top = y_of(max(opening, closing))
        # Свеча без хода цены всё равно должна быть видна чертой.
        body_height = max(2.0, y_of(min(opening, closing)) - body_top)
        canvas.rect(middle - body_width / 2, body_top, body_width, body_height, color)
    return canvas.to_png()
