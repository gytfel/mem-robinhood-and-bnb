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
