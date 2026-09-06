"""Фильтры безопасности: как отчёт сопоставляется с настройками пользователя."""

from __future__ import annotations

from decimal import Decimal

from sniperbot.chain.dex import PairState
from sniperbot.chain.erc20 import TokenInfo
from sniperbot.db.models import ChainSettings
from sniperbot.sniper.safety import SafetyReport, SimulationResult, evaluate_for_settings

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
PAIR = "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"


def make_report(
    *, liquidity=Decimal("5"), buy_tax=200, sell_tax=200, can_sell=True, can_buy=True,
    lp_burned=Decimal("100"), owner=None, available=True,
) -> SafetyReport:
    token = TokenInfo(address=TOKEN, name="Meme", symbol="MEME", decimals=18, owner=owner)
    report = SafetyReport(token=token, chain_key="bsc", router="0x0", pair=PAIR)
    report.pair_state = PairState(
        pair=PAIR, token=TOKEN, wrapped_native="0x0",
        reserve_token=10**24, reserve_native=int(liquidity * 10**18),
    )
    report.liquidity_native = liquidity
    report.lp_burned = lp_burned
    report.simulation = SimulationResult(
        available=available, can_buy=can_buy, can_sell=can_sell,
        buy_tax_bps=buy_tax, sell_tax_bps=sell_tax,
    )
    return report


def cfg(**overrides) -> ChainSettings:
    defaults = {
        "min_liquidity": Decimal("2"), "max_liquidity": Decimal("0"),
        "max_buy_tax_bps": 1000, "max_sell_tax_bps": 1000,
        "honeypot_check": True, "require_simulation": True,
        "require_renounced": False, "min_lp_burned_pct": 0,
    }
    defaults.update(overrides)
    return ChainSettings(user_id=1, chain="bsc", **defaults)


def test_good_token_passes():
    ok, reasons = evaluate_for_settings(make_report(), cfg())
    assert ok is True
    assert reasons == []


def test_honeypot_is_rejected():
    ok, reasons = evaluate_for_settings(make_report(can_sell=False), cfg())
    assert ok is False
    assert any("honeypot" in reason for reason in reasons)


def test_low_liquidity_is_rejected():
    ok, reasons = evaluate_for_settings(make_report(liquidity=Decimal("0.5")), cfg())
    assert ok is False
    assert any("ликвидность" in reason for reason in reasons)


def test_max_liquidity_filter():
    ok, _ = evaluate_for_settings(make_report(liquidity=Decimal("500")), cfg(max_liquidity=Decimal("100")))
    assert ok is False


def test_high_tax_is_rejected():
    ok, reasons = evaluate_for_settings(make_report(sell_tax=5000), cfg())
    assert ok is False
    assert any("налог на продажу" in reason for reason in reasons)


def test_simulation_required_but_unavailable():
    report = make_report(available=False, can_sell=None, can_buy=None)
    report.simulation.error = "RPC не поддерживает state override"
    ok, reasons = evaluate_for_settings(report, cfg())
    assert ok is False
    # без требования симуляции токен проходит
    ok2, _ = evaluate_for_settings(report, cfg(require_simulation=False))
    assert ok2 is True


def test_renounce_requirement():
    report = make_report(owner="0x1111111111111111111111111111111111111111")
    assert evaluate_for_settings(report, cfg())[0] is True
    ok, reasons = evaluate_for_settings(report, cfg(require_renounced=True))
    assert ok is False
    assert any("владелец" in reason for reason in reasons)


def test_lp_burn_requirement():
    report = make_report(lp_burned=Decimal("10"))
    ok, reasons = evaluate_for_settings(report, cfg(min_lp_burned_pct=90))
    assert ok is False
    assert any("LP" in reason for reason in reasons)


def test_empty_pair_is_rejected():
    report = make_report()
    report.pair_state = None
    ok, reasons = evaluate_for_settings(report, cfg())
    assert ok is False
    assert reasons == ["нет ликвидности"]


def test_verdict_and_score():
    from sniperbot.sniper.safety import Check

    report = make_report()
    report.checks = [
        Check("a", "Проверка A", True, critical=True),
        Check("b", "Проверка B", True),
    ]
    assert report.verdict == "safe"
    assert report.score == 100

    report.checks.append(Check("c", "Проверка C", False, critical=True))
    assert report.verdict == "danger"
    assert report.blocking[0].key == "c"
    assert report.score < 100
