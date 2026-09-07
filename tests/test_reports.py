"""Отчёты по сделкам: сводки, списки плюсовых/убыточных и выгрузка в файл."""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal

import pytest

from sniperbot.db.models import Position
from sniperbot.reports import (
    EXIT_TITLES,
    render_report,
    render_summary,
    summarize,
    to_rows,
    trades_csv,
)
from sniperbot.utils.fmt import to_wei

NOW = dt.datetime(2026, 9, 7, 12, 0, tzinfo=dt.UTC)


def trade(symbol: str, spent: str, returned: str, *, paper: bool = False,
          reason: str = "take_profit", **kwargs) -> Position:
    defaults = {
        "user_id": 1, "chain": "bsc", "token_address": "0x" + "1" * 40,
        "router_address": "0x2", "status": "closed", "token_symbol": symbol,
        "native_spent_wei": to_wei(spent), "native_returned_wei": to_wei(returned),
        "is_paper": paper, "exit_reason": reason, "opened_at": NOW - dt.timedelta(hours=2),
        "closed_at": NOW, "dex_kind": "v2", "pool_fee": 0, "source": "auto", "ab_group": "",
    }
    defaults.update(kwargs)
    return Position(**defaults)


def sample() -> list[Position]:
    return [
        trade("WIN1", "0.1", "0.35"),                              # +250%
        trade("WIN2", "0.1", "0.15"),                              # +50%
        trade("LOSS1", "0.1", "0.04", reason="stop_loss"),         # −60%
        trade("LOSS2", "0.1", "0.0", reason="rug"),                # −100%
        trade("LOSS3", "0.2", "0.19", reason="dead"),              # −5%
    ]


# ------------------------------------------------------------------- сводка
def test_summary_splits_wins_and_losses():
    summary = summarize(sample(), "Боевой")

    assert summary.count == 5
    assert [row.position.token_symbol for row in summary.wins] == ["WIN1", "WIN2"]
    # убытки идут от самого крупного к самому мелкому
    assert [row.position.token_symbol for row in summary.losses] == ["LOSS2", "LOSS1", "LOSS3"]
    assert summary.winrate == 40


def test_summary_totals_are_correct():
    summary = summarize(sample(), "Боевой")

    assert summary.spent == Decimal("0.6")
    assert summary.returned == Decimal("0.73")
    assert summary.pnl == Decimal("0.13")
    assert round(summary.pnl_pct, 2) == Decimal("21.67")
    assert summary.average == Decimal("0.13") / 5


def test_average_win_and_loss():
    summary = summarize(sample(), "Боевой")
    assert summary.avg_win == (Decimal("0.25") + Decimal("0.05")) / 2
    assert summary.avg_loss == (Decimal("-0.06") + Decimal("-0.1") + Decimal("-0.01")) / 3


def test_percentages_per_trade():
    rows = {row.position.token_symbol: row for row in to_rows(sample())}
    assert rows["WIN1"].pnl_pct == Decimal(250)
    assert rows["LOSS2"].pnl_pct == Decimal(-100)
    assert rows["LOSS1"].profitable is False


def test_empty_summary_does_not_divide_by_zero():
    summary = summarize([], "Пусто")
    assert summary.count == 0
    assert summary.winrate == 0
    assert summary.pnl == 0
    assert summary.average == 0
    assert summary.pnl_pct == 0
    assert "сделок нет" in render_summary(summary, "BNB")


def test_loss_reasons_are_counted():
    summary = summarize(sample(), "Боевой")
    reasons = dict(summary.exit_reasons(profitable=False))
    assert reasons[EXIT_TITLES["stop_loss"]] == 1
    assert reasons[EXIT_TITLES["rug"]] == 1
    assert reasons[EXIT_TITLES["dead"]] == 1


# -------------------------------------------------------------------- текст
def test_rendered_summary_lists_both_sides():
    text = render_summary(summarize(sample(), "💰 Боевой режим"), "BNB")

    assert "Плюсовые" in text and "Убыточные" in text
    assert "WIN1" in text and "LOSS2" in text
    assert "+250%" in text
    assert "-100%" in text or "−100%" in text
    assert "стоп-лосс" in text          # причина убытка попадает в отчёт


def test_long_lists_are_truncated_with_a_hint():
    many = [trade(f"W{i}", "0.1", "0.5") for i in range(9)]
    text = render_summary(summarize(many, "Боевой"), "BNB", top=5)
    assert "и ещё 4" in text
    assert "в файле" in text


def test_report_compares_both_modes():
    real = summarize(sample(), "💰 Боевой режим")
    paper = summarize([trade("P1", "0.1", "0.6", paper=True)], "🧪 Тестовый режим")

    text = render_report(real, paper, days=30, symbol="BNB", open_positions=2)

    assert "Боевой режим" in text and "Тестовый режим" in text
    assert "Открытых позиций сейчас: <b>2</b>" in text
    assert "тестовые" in text            # бумажные идут лучше в этом наборе
    assert "в файле" in text


def test_report_without_trades_still_renders():
    text = render_report(summarize([], "💰"), summarize([], "🧪"), days=7, symbol="BNB")
    assert "сделок нет" in text


# --------------------------------------------------------------------- файл
def parse_csv(content: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(content), delimiter=";"))


def test_csv_contains_every_trade_with_mode_and_reason():
    rows = to_rows(sample() + [trade("PAPER", "0.1", "0.2", paper=True)])
    parsed = parse_csv(trades_csv(rows))

    assert len(parsed) == 6
    assert {row["режим"] for row in parsed} == {"боевой", "тест"}
    loss = next(row for row in parsed if row["токен"] == "LOSS1")
    assert loss["причина выхода"] == "стоп-лосс"
    assert loss["pnl"].startswith("-0,06")
    assert loss["статус"] == "закрыта"


def test_csv_includes_open_positions_separately():
    open_position = trade("OPEN", "0.1", "0", status="open", closed_at=None, exit_reason="")
    content = trades_csv(to_rows(sample()), to_rows([open_position]))
    parsed = parse_csv(content)

    assert parsed[-1]["токен"] == "OPEN"
    assert parsed[-1]["статус"] == "открыта"
    assert parsed[-1]["закрыта"] == ""
    assert parsed[-1]["причина выхода"] == ""


def test_csv_uses_comma_decimals_for_excel():
    content = trades_csv(to_rows([trade("X", "0.1", "0.25")]))
    row = parse_csv(content)[0]
    assert "," in row["вложено"] and "." not in row["вложено"]
    assert row["pnl_%"] == "150,00"


def test_csv_header_is_stable():
    header = trades_csv([]).splitlines()[0].split(";")
    assert header[:6] == ["статус", "режим", "открыта", "закрыта", "сеть", "токен"]


@pytest.mark.parametrize("reason", list(EXIT_TITLES))
def test_every_exit_reason_has_a_human_title(reason):
    row = to_rows([trade("X", "0.1", "0.2", reason=reason)])[0]
    assert row.exit_title
    assert not row.exit_title.startswith("take_")     # в отчёте только по-русски
