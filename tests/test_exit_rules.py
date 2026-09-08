"""Правила выхода из позиции: приоритеты, лестница, безубыток, защита от слива."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniperbot.db.models import Position
from sniperbot.sniper.positions import ExitContext, decide_exit, ladder_percent
from sniperbot.utils.fmt import to_wei


def position(**kwargs) -> Position:
    defaults = {
        "id": 1, "user_id": 1, "chain": "bsc", "token_address": "0x1", "token_symbol": "MEME",
        "take_profit_pct": 100, "stop_loss_pct": 50, "trailing_stop_pct": 0,
        "auto_sell": True, "sell_percent": 100, "entry_price": Decimal("1"),
        "amount_wei": to_wei(100), "bought_wei": to_wei(100),
        "tp_ladder": "", "tp_done": "", "breakeven_pct": 0, "breakeven_armed": False,
        "rug_guard_pct": 0, "dead_timeout_min": 0, "dead_min_pct": 0,
    }
    defaults.update(kwargs)
    return Position(**defaults)


def ctx(change, *, peak_change=None, price=None, peak_price=None,
        liquidity=None, peak_liquidity=None, age_minutes=0.0) -> ExitContext:
    change = Decimal(str(change))
    price = Decimal(str(price)) if price is not None else Decimal(1) + change / 100
    peak_change = Decimal(str(peak_change)) if peak_change is not None else max(change, Decimal(0))
    peak_price = Decimal(str(peak_price)) if peak_price is not None else Decimal(1) + peak_change / 100
    return ExitContext(
        change=change, peak_change=peak_change, price=price, peak_price=peak_price,
        liquidity=Decimal(str(liquidity)) if liquidity is not None else None,
        peak_liquidity=Decimal(str(peak_liquidity)) if peak_liquidity is not None else None,
        age_minutes=age_minutes,
    )


# ------------------------------------------------------------------ базовые
def test_take_profit_triggers():
    rule, percent, _ = decide_exit(position(), ctx(120))
    assert rule.key == "take_profit"
    assert percent == 100


def test_partial_take_profit():
    rule, percent, _ = decide_exit(position(sell_percent=50), ctx(100))
    assert rule.key == "take_profit"
    assert percent == 50


def test_stop_loss_triggers():
    rule, _, _ = decide_exit(position(), ctx(-60))
    assert rule.key == "stop_loss"


def test_nothing_triggers_inside_band():
    rule, percent, _ = decide_exit(position(), ctx(20))
    assert rule is None
    assert percent == 0


def test_disabled_autosell_holds_everything():
    assert decide_exit(position(auto_sell=False), ctx(500))[0] is None
    assert decide_exit(position(auto_sell=False), ctx(-90))[0] is None


# --------------------------------------------------------------- приоритеты
def test_liquidity_drain_beats_every_other_rule():
    """Слив ликвидности — самая срочная причина выйти, даже в прибыли."""
    rule, percent, _ = decide_exit(
        position(rug_guard_pct=50, take_profit_pct=100),
        ctx(300, liquidity=2, peak_liquidity=10),
    )
    assert rule.key == "rug"
    assert percent == 100


def test_small_liquidity_dip_is_tolerated():
    rule, _, _ = decide_exit(
        position(rug_guard_pct=50), ctx(10, liquidity=8, peak_liquidity=10)
    )
    assert rule is None


def test_stop_loss_has_priority_over_take_profit():
    pos = position(take_profit_pct=1, stop_loss_pct=1)
    assert decide_exit(pos, ctx(-5))[0].key == "stop_loss"


# ---------------------------------------------------------------- безубыток
def test_breakeven_exit_when_armed_and_price_returns():
    pos = position(breakeven_pct=50, breakeven_armed=True, stop_loss_pct=90)
    rule, percent, _ = decide_exit(pos, ctx(-1))
    assert rule.key == "breakeven"
    assert percent == 100


def test_breakeven_does_not_fire_before_arming():
    pos = position(breakeven_pct=50, breakeven_armed=False, stop_loss_pct=90)
    assert decide_exit(pos, ctx(-1))[0] is None


def test_breakeven_holds_while_in_profit():
    pos = position(breakeven_pct=50, breakeven_armed=True, take_profit_pct=0)
    assert decide_exit(pos, ctx(30))[0] is None


# ----------------------------------------------------------------- лестница
def test_ladder_fires_step_by_step():
    pos = position(tp_ladder="100:50,300:30", take_profit_pct=0)

    rule, percent, marker = decide_exit(pos, ctx(120))
    assert rule.key == "ladder"
    assert marker == "100"
    assert percent == 50                      # половина исходного объёма

    pos.tp_done = "100"
    assert decide_exit(pos, ctx(120))[0] is None   # ступень уже сработала

    rule, _, marker = decide_exit(pos, ctx(350))
    assert marker == "300"


def test_ladder_percent_counts_from_original_size():
    """Ступень «30% позиции» после первой продажи — это больше 30% остатка."""
    pos = position(amount_wei=to_wei(50), bought_wei=to_wei(100))
    assert ladder_percent(pos, 30) == 60           # 30 из 100 = 60% от оставшихся 50
    assert ladder_percent(pos, 50) == 100          # запрошено больше, чем осталось


def test_ladder_replaces_plain_take_profit():
    pos = position(tp_ladder="200:40", take_profit_pct=100)
    rule, _, _ = decide_exit(pos, ctx(150))
    assert rule is None                            # обычный TP отключён лестницей
    assert decide_exit(pos, ctx(250))[0].key == "ladder"


# ------------------------------------------------------------- трейлинг/тайм
def test_trailing_stop_triggers_after_peak():
    pos = position(take_profit_pct=0, trailing_stop_pct=20)
    rule, percent, _ = decide_exit(pos, ctx(120, peak_change=200, price=2.2, peak_price=3.0))
    assert rule.key == "trailing"
    assert percent == 100


def test_trailing_does_not_fire_in_loss():
    pos = position(take_profit_pct=0, stop_loss_pct=0, trailing_stop_pct=20)
    assert decide_exit(pos, ctx(-30, peak_change=0, price=0.7, peak_price=1.0))[0] is None


def test_dead_position_is_closed_after_timeout():
    pos = position(take_profit_pct=0, stop_loss_pct=0, dead_timeout_min=30, dead_min_pct=20)
    assert decide_exit(pos, ctx(5, age_minutes=10))[0] is None          # рано
    rule, _, _ = decide_exit(pos, ctx(5, age_minutes=45))
    assert rule.key == "dead"


def test_growing_position_survives_the_timeout():
    pos = position(take_profit_pct=0, stop_loss_pct=0, dead_timeout_min=30, dead_min_pct=20)
    assert decide_exit(pos, ctx(10, peak_change=80, age_minutes=60))[0] is None


@pytest.mark.parametrize("percent", [1, 50, 100])
def test_exit_percent_is_always_valid(percent):
    pos = position(sell_percent=percent)
    _rule, value, _ = decide_exit(pos, ctx(500))
    assert 1 <= value <= 100


# --------------------------------------------- частота проверки позиции
def test_fresh_positions_are_polled_often():
    """Свежая позиция проверяется часто: именно там теряются проценты."""
    from sniperbot.sniper.positions import check_interval

    assert check_interval(0.0, 1.5, 6.0, 15.0) == 1.5      # только что купили
    assert check_interval(14.9, 1.5, 6.0, 15.0) == 1.5
    assert check_interval(15.1, 1.5, 6.0, 15.0) == 6.0     # позиция «остыла»
    assert check_interval(600.0, 1.5, 6.0, 15.0) == 6.0


def test_fast_window_can_be_disabled():
    from sniperbot.sniper.positions import check_interval

    assert check_interval(0.0, 1.5, 6.0, 0.0) == 6.0       # окно выключено
