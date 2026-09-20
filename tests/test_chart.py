"""График цены в карточке позиции.

Картинка здесь текстовая, и проверять в ней надо не красоту, а честность:
сколько времени она охватывает, не врёт ли форма и не ломается ли на краях —
на одной точке, на неподвижной цене, на испорченной строке.
"""

from __future__ import annotations

from decimal import Decimal

from sniperbot.chart import (
    LEVELS,
    MAX_POINTS,
    MIN_STEP_SECONDS,
    add_point,
    pack,
    points,
    render,
    sparkline,
    step_for,
)


def series(*prices: str) -> list[Decimal]:
    return [Decimal(value) for value in prices]


# ----------------------------------------------------------- запись замеров
def test_a_second_measurement_too_soon_is_not_kept():
    """Опрос идёт раз в шесть секунд: писать каждый — раздуть строку впустую."""
    track = add_point("", 0, Decimal("1"))
    assert add_point(track, 5, Decimal("2")) == track


def test_a_measurement_after_the_step_is_kept():
    track = add_point("", 0, Decimal("1"))
    grown = add_point(track, MIN_STEP_SECONDS, Decimal("2"))
    assert len(points(grown)) == 2


def test_the_step_grows_with_the_position_age():
    """Иначе график часовой позиции показывал бы последние четверть часа."""
    assert step_for(0) == MIN_STEP_SECONDS
    assert step_for(60 * 60) == 60 * 60 / MAX_POINTS, "час жизни — точка раз в две минуты"
    assert step_for(24 * 60 * 60) > 40 * MIN_STEP_SECONDS, "сутки — раз в сорок минут"


def test_the_chart_covers_the_whole_life_not_the_last_minutes():
    track = ""
    for second in range(0, 4 * 60 * 60, 10):        # четыре часа, замер раз в 10 с
        track = add_point(track, second, Decimal(1 + second % 7))
    rows = points(track)
    assert len(rows) <= MAX_POINTS, "строка обязана оставаться короткой"
    assert rows[0][0] < 60, "начало жизни позиции должно остаться на графике"
    assert rows[-1][0] > 3 * 60 * 60, "как и её конец"


def test_a_price_of_nothing_is_not_a_measurement():
    assert add_point("", 0, None) == ""
    assert add_point("", 0, Decimal("0")) == ""


def test_a_broken_track_does_not_break_the_card():
    assert points("мусор,и:ещё,10:1") == [(10, Decimal("1"))]
    assert render("мусор") == ""


def test_tiny_prices_survive_the_round_trip():
    """Мемкоины стоят 3e-8 — округление до нуля убило бы весь график."""
    track = pack([(0, Decimal("3.317e-8")), (60, Decimal("2.183e-8"))])
    low, high = (price for _, price in points(track))
    assert low > high > 0
    assert high == Decimal("2.183e-8")


# ------------------------------------------------------------- сама картинка
def test_the_shape_follows_the_prices():
    line = sparkline(series("1", "2", "3", "4"))
    assert line[0] == LEVELS[0] and line[-1] == LEVELS[-1]
    assert list(line) == sorted(line, key=LEVELS.index), "рост рисуем ростом"


def test_a_fall_is_drawn_as_a_fall():
    line = sparkline(series("4", "3", "2", "1"))
    assert line[0] == LEVELS[-1] and line[-1] == LEVELS[0]


def test_a_motionless_price_is_drawn_flat():
    line = sparkline(series("5", "5", "5"))
    assert line == LEVELS[len(LEVELS) // 2] * 3, "деление на ноль тут недопустимо"


def test_one_point_is_not_a_chart():
    assert sparkline(series("1")) == ""
    assert render(pack([(0, Decimal("1"))])) == ""


def test_the_chart_says_how_much_time_it_covers():
    track = pack([(0, Decimal("1")), (600, Decimal("2"))])
    drawn = render(track)
    assert "10 мин" in drawn
    assert "<code>" in drawn, "без моноширинного шрифта блоки разъезжаются"


def test_a_very_young_position_says_so_instead_of_zero_minutes():
    track = pack([(0, Decimal("1")), (20, Decimal("2"))])
    assert "меньше минуты" in render(track)


# ------------------------------------------------------------- в карточке
def test_the_card_shows_the_chart():
    from sniperbot.bot.views import render_position
    from sniperbot.config import ChainConfig
    from sniperbot.db.models import Position
    from sniperbot.utils.fmt import to_wei

    chain = ChainConfig(key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH",
                        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40)
    position = Position(
        id=7, user_id=1, chain="rh", token_address="0x" + "a" * 40, token_symbol="PAW",
        token_decimals=18, router_address="0x" + "c" * 40, amount_wei=to_wei(1000),
        native_spent_wei=to_wei("0.008"), native_returned_wei=0, status="open",
        entry_price=Decimal("3.317e-8"), last_price=Decimal("2.183e-8"),
        price_track=pack([(0, Decimal("3.3e-8")), (300, Decimal("4.1e-8")),
                          (600, Decimal("2.2e-8"))]),
    )

    text = render_position(position, chain, position.last_price)
    assert "📈" in text and "10 мин" in text


def test_a_card_without_measurements_looks_as_before():
    from sniperbot.bot.views import render_position
    from sniperbot.config import ChainConfig
    from sniperbot.db.models import Position
    from sniperbot.utils.fmt import to_wei

    chain = ChainConfig(key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH",
                        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40)
    position = Position(id=8, user_id=1, chain="rh", token_address="0x" + "a" * 40,
                        token_symbol="PAW", token_decimals=18, router_address="0x" + "c" * 40,
                        amount_wei=to_wei(1000), native_spent_wei=to_wei("0.008"),
                        native_returned_wei=0, status="open", price_track="")

    assert "📈" not in render_position(position, chain, None)


# ------------------------------------------------------------------ свечи
def track_of(*prices: str, step: int = 60) -> str:
    from sniperbot.chart import pack

    return pack([(index * step, Decimal(value)) for index, value in enumerate(prices)])


def test_a_candle_opens_where_the_previous_one_closed():
    """Разрыв между свечами означал бы скачок цены, которого не было."""
    from sniperbot.chart import candles

    bars = candles(track_of("1", "2", "3", "4"), count=4)
    for earlier, later in zip(bars, bars[1:], strict=False):
        assert later[0] == earlier[3]


def test_a_candle_holds_the_high_and_the_low_of_its_period():
    from sniperbot.chart import candles

    bars = candles(track_of("10", "15", "8", "12"), count=2)
    highs = [bar[1] for bar in bars]
    lows = [bar[2] for bar in bars]
    assert max(highs) == Decimal("15") and min(lows) == Decimal("8")


def test_a_period_without_trades_is_drawn_as_a_line_not_a_gap():
    from sniperbot.chart import candles, pack

    # замеры только в начале и в конце часа — середина пустая
    bars = candles(pack([(0, Decimal("5")), (60, Decimal("5")), (3600, Decimal("7"))]), count=6)
    assert len(bars) >= 5, "пустые промежутки не выбрасываем"
    quiet = bars[2]
    assert quiet[0] == quiet[1] == quiet[2] == quiet[3], "цена не менялась — свеча-черта"


def test_too_few_measurements_are_not_a_chart():
    from sniperbot.chart import candles, draw, pack

    assert candles(pack([(0, Decimal("1"))])) == []
    assert draw(pack([(0, Decimal("1"))]), Decimal("1")) is None


# ------------------------------------------------------------------ картинка
def decode(png: bytes) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    """Свой маленький читатель PNG: проверять картинку по её же коду нечестно."""
    import struct
    import zlib

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    offset = 8
    while offset < len(png):
        size = struct.unpack(">I", png[offset:offset + 4])[0]
        kind = png[offset + 4:offset + 8]
        body = png[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", png[offset + 8 + size:offset + 12 + size])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF, f"битая контрольная сумма {kind}"
        chunks.setdefault(kind, b"")
        chunks[kind] += body
        offset += 12 + size
    width, height, depth, kind = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, kind) == (8, 2), "восемь бит на канал, три канала"
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = []
    stride = width * 3 + 1
    for index in range(height):
        line = raw[index * stride:(index + 1) * stride]
        assert line[0] == 0, "фильтр не используем"
        rows.append([tuple(line[1 + x * 3:4 + x * 3]) for x in range(width)])
    return width, height, rows


def test_the_canvas_writes_a_readable_png():
    from sniperbot.utils.png import Canvas

    canvas = Canvas(6, 4, (0, 0, 0))
    canvas.rect(1, 1, 2, 2, (255, 128, 0))
    width, height, rows = decode(canvas.to_png())
    assert (width, height) == (6, 4)
    assert rows[1][1] == (255, 128, 0) and rows[2][2] == (255, 128, 0)
    assert rows[0][0] == (0, 0, 0) and rows[3][5] == (0, 0, 0)


def test_what_falls_off_the_canvas_does_not_wrap_around():
    from sniperbot.utils.png import Canvas

    canvas = Canvas(4, 3, (0, 0, 0))
    canvas.rect(-5, -5, 3, 3, (255, 255, 255))
    canvas.rect(3, 2, 10, 10, (255, 255, 255))
    _, _, rows = decode(canvas.to_png())
    assert rows[0][0] == (0, 0, 0), "прямоугольник слева за краем не должен рисоваться"
    assert rows[2][3] == (255, 255, 255)
    assert rows[0][3] == (0, 0, 0)


def test_the_drawn_chart_is_green_when_the_price_rises():
    from sniperbot.chart import UP, draw

    png = draw(track_of("1", "2", "3", "4", "5", "6"), None, count=6)
    _, _, rows = decode(png)
    painted = {pixel for row in rows for pixel in row}
    assert UP in painted, "растущие свечи рисуем зелёным"


def test_the_entry_line_is_inside_the_picture():
    """Вход выше всей цены — без запаса линия оказалась бы за краем кадра."""
    from sniperbot.chart import ENTRY_LINE, draw

    png = draw(track_of("1", "2", "3"), Decimal("50"), count=3)
    _, _, rows = decode(png)
    painted = {pixel for row in rows for pixel in row}
    assert ENTRY_LINE in painted
