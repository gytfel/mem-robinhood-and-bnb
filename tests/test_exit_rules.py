"""Правила автоматического выхода из позиции."""

from __future__ import annotations

from decimal import Decimal

from sniperbot.db.models import Position
from sniperbot.sniper.positions import PositionMonitor

monitor = PositionMonitor(registry=None, trader=None, notifier=None, settings=None)  # type: ignore[arg-type]


def position(**kwargs) -> Position:
    defaults = {
        "id": 1, "user_id": 1, "chain": "bsc", "token_address": "0x1", "token_symbol": "MEME",
        "take_profit_pct": 100, "stop_loss_pct": 50, "trailing_stop_pct": 0,
        "auto_sell": True, "sell_percent": 100, "entry_price": Decimal("1"),
    }
    defaults.update(kwargs)
    return Position(**defaults)


def rule(pos, change, price, peak):
    return monitor._exit_rule(pos, Decimal(str(change)), Decimal(str(price)), Decimal(str(peak)))


def test_take_profit_triggers():
    trigger, percent = rule(position(), 120, 2.2, 2.2)
    assert trigger.key == "take_profit"
    assert percent == 100


def test_take_profit_partial():
    trigger, percent = rule(position(sell_percent=50), 100, 2, 2)
    assert trigger.key == "take_profit"
    assert percent == 50


def test_stop_loss_triggers():
    trigger, _ = rule(position(), -60, 0.4, 1)
    assert trigger.key == "stop_loss"


def test_nothing_triggers_inside_band():
    trigger, percent = rule(position(), 20, 1.2, 1.2)
    assert trigger is None
    assert percent == 0


def test_trailing_stop_triggers_after_peak():
    pos = position(take_profit_pct=0, trailing_stop_pct=20)
    # цена доходила до 3.0, сейчас 2.2 — откат 26% при плюсовой позиции
    trigger, percent = rule(pos, 120, 2.2, 3.0)
    assert trigger.key == "trailing"
    assert percent == 100


def test_trailing_does_not_fire_in_loss():
    pos = position(take_profit_pct=0, stop_loss_pct=0, trailing_stop_pct=20)
    trigger, _ = rule(pos, -30, 0.7, 1.0)
    assert trigger is None


def test_stop_loss_has_priority_over_take_profit():
    """Одновременно сработать не могут: сначала проверяется стоп-лосс."""
    pos = position(take_profit_pct=1, stop_loss_pct=1)
    trigger, _ = rule(pos, -5, 0.95, 1)
    assert trigger.key == "stop_loss"


def test_disabled_rules_do_nothing():
    pos = position(take_profit_pct=0, stop_loss_pct=0, trailing_stop_pct=0)
    assert rule(pos, 500, 6, 6)[0] is None
    assert rule(pos, -90, 0.1, 1)[0] is None
