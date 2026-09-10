"""Отчёт о качестве фильтров: расчёты и осторожность выводов."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from sniperbot.pairstats import (
    Bucket,
    Outcome,
    compare_verdict,
    filter_rows,
    hour_rows,
    hours_verdict,
    peak_multiple,
    render_outcomes,
    round_trip_cost,
    significant,
    split,
    to_outcomes,
)

NOW = dt.datetime(2026, 9, 9, 21, tzinfo=dt.UTC)


def pair(status="rejected", codes="", first="1", peak="1", hour=21):
    return SimpleNamespace(
        status=status, reject_codes=codes,
        first_price=Decimal(first) if first else None,
        peak_price=Decimal(peak) if peak else None,
        created_at=NOW.replace(hour=hour),
    )


def outcomes(accepted: int, accepted_hits: int, denied: int, denied_hits: int,
             codes: str = "min_liquidity", hour: int = 21) -> list[Outcome]:
    rows = []
    for index in range(accepted):
        rows.append(Outcome(accepted=True, hour=hour,
                            multiple=Decimal(3) if index < accepted_hits else Decimal(1)))
    for index in range(denied):
        rows.append(Outcome(accepted=False, hour=hour, codes=tuple(codes.split(",")),
                            multiple=Decimal(3) if index < denied_hits else Decimal(1)))
    return rows


# --------------------------------------------------------------------- разбор
def test_peak_multiple_needs_both_prices():
    assert peak_multiple(pair(first="2", peak="5")) == Decimal("2.5")
    assert peak_multiple(pair(first=None, peak="5")) is None
    assert peak_multiple(pair(first="0", peak="5")) is None


def test_to_outcomes_skips_pools_without_measurements():
    rows = to_outcomes([pair(first="1", peak="2"), pair(first=None, peak=None)])
    assert len(rows) == 1
    assert rows[0].multiple == Decimal(2)


def test_sniped_pool_counts_as_accepted_even_with_stale_codes():
    """Купленный пул прошёл фильтры — что бы ни лежало в старых кодах."""
    rows = to_outcomes([pair(status="sniped", codes="proxy", first="1", peak="2")])
    assert rows[0].accepted is True


def test_pool_without_codes_and_without_rejection_is_accepted():
    rows = to_outcomes([pair(status="watch", codes="", first="1", peak="2")])
    assert rows[0].accepted is True


# --------------------------------------------------------------------- группы
def test_bucket_median_and_milestones():
    bucket = Bucket("тест", [
        Outcome(True, Decimal(1)), Outcome(True, Decimal(2)),
        Outcome(True, Decimal(3)), Outcome(True, Decimal(6)),
    ])   # low не задан: по умолчанию 1× — просадки не было
    assert bucket.median == Decimal("2.5")
    assert bucket.share(Decimal(2)) == Decimal(75)
    assert bucket.share(Decimal(5)) == Decimal(25)


def test_empty_bucket_does_not_divide_by_zero():
    empty = Bucket("пусто")
    assert empty.share() == Decimal(0)
    assert empty.median == Decimal(0)


# --------------------------------------------------------- проверка значимости
def test_small_samples_are_never_significant():
    """11% против 8% на полусотне наблюдений — это шум, а не находка."""
    assert significant(3, 28, 1100, 13678) is False


def test_large_clear_difference_is_significant():
    assert significant(220, 1000, 70, 1000) is True


def test_identical_proportions_are_not_significant():
    assert significant(100, 1000, 100, 1000) is False


def test_verdict_admits_uncertainty_on_thin_data():
    rows = outcomes(accepted=10, accepted_hits=2, denied=12, denied_hits=1)
    passed, denied = split(rows)
    assert "в пределах погрешности" in compare_verdict(passed, denied)


def test_verdict_confirms_filters_when_difference_is_real():
    rows = outcomes(accepted=400, accepted_hits=120, denied=400, denied_hits=20)
    passed, denied = split(rows)
    assert "фильтры работают" in compare_verdict(passed, denied)


def test_verdict_warns_when_filters_cut_the_winners():
    """Главный случай, ради которого отчёт и нужен: фильтр режет прибыль."""
    rows = outcomes(accepted=400, accepted_hits=20, denied=400, denied_hits=160)
    passed, denied = split(rows)
    verdict = compare_verdict(passed, denied)
    assert "режут то, что растёт" in verdict
    assert "/config filters" in verdict


# ------------------------------------------------------------ разрез по фильтрам
def test_filter_marked_green_when_it_drops_weak_tokens():
    rows = outcomes(accepted=300, accepted_hits=90, denied=300, denied_hits=15)
    passed, denied = split(rows)
    row = filter_rows(denied, passed)[0]
    assert row.mark == "🟢"
    assert row.title == "ликвидности меньше минимума"
    assert row.count == 300


def test_filter_marked_red_when_it_drops_winners():
    rows = outcomes(accepted=300, accepted_hits=15, denied=300, denied_hits=120)
    passed, denied = split(rows)
    assert filter_rows(denied, passed)[0].mark == "🔴"


def test_filter_marked_grey_on_thin_data():
    rows = outcomes(accepted=20, accepted_hits=4, denied=15, denied_hits=1)
    passed, denied = split(rows)
    assert filter_rows(denied, passed)[0].mark == "⚪️"


def test_pool_rejected_by_several_filters_counts_in_each():
    rows = outcomes(accepted=50, accepted_hits=10, denied=40, denied_hits=8,
                    codes="proxy,mintable")
    passed, denied = split(rows)
    codes = {row.code: row.count for row in filter_rows(denied, passed)}
    assert codes == {"proxy": 40, "mintable": 40}


# --------------------------------------------------------------- разрез по часам
def test_hours_need_a_minimum_sample():
    rows = outcomes(accepted=5, accepted_hits=1, denied=5, denied_hits=1, hour=3)
    assert hour_rows(rows) == []


def test_hours_are_ranked_by_success_share():
    rows = outcomes(accepted=0, accepted_hits=0, denied=40, denied_hits=20, hour=21)
    rows += outcomes(accepted=0, accepted_hits=0, denied=40, denied_hits=4, hour=11)
    ranked = hour_rows(rows)
    assert [row.hour for row in ranked] == [21, 11]
    assert ranked[0].share == Decimal(50)


def test_hours_verdict_stays_quiet_without_enough_hours():
    rows = outcomes(accepted=0, accepted_hits=0, denied=40, denied_hits=20, hour=21)
    assert "мало" in hours_verdict(hour_rows(rows), rows)


# -------------------------------------------------------------------- издержки
def test_round_trip_cost_in_coin_and_percent():
    cost, share = round_trip_cost(gas_used=600_000, gas_price_wei=3 * 10**9,
                                  buy_amount=Decimal("0.05"))
    assert cost == Decimal("0.0018")
    assert share == Decimal("3.6")


def test_round_trip_cost_survives_zero_entry():
    _, share = round_trip_cost(600_000, 10**9, Decimal(0))
    assert share == Decimal(0)


# ---------------------------------------------------------------------- отчёт
def test_report_reads_as_a_whole():
    rows = outcomes(accepted=200, accepted_hits=60, denied=500, denied_hits=25)
    text = render_outcomes(rows, seen=9000, bought=14, window="за 24 ч")

    assert "Качество фильтров" in text
    assert "Видел пулов: 9000" in text
    assert "Прошли фильтр</b> (200 шт)" in text
    assert "Отклонены фильтром</b> (500 шт)" in text
    assert "фильтры работают" in text
    assert "ликвидности меньше минимума" in text


def test_report_without_measurements_is_not_a_crash():
    assert "замеров нет" in render_outcomes([], seen=0, bought=0, window="за 24 ч")


# ------------------------------------------------------------------- хранение
async def test_price_tracking_keeps_first_and_peak(db):
    """Судьба пула пишется по всем найденным, иначе сравнивать будет нечего."""
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope

    async with session_scope() as session:
        row = await repo.add_seen_pair(session, chain="bsc", pair_address="0xpool",
                                       token_address="0xtoken", status="rejected")
        pair_id = row.id

    for price in ("1.0", "2.5", "1.2"):
        async with session_scope() as session:
            await repo.track_pool_price(session, pair_id, Decimal(price))

    async with session_scope() as session:
        tracked = await repo.outcome_pairs(session, "bsc")

    assert len(tracked) == 1
    assert tracked[0].first_price == Decimal(1)
    assert tracked[0].peak_price == Decimal("2.5")   # откат пик не сбрасывает
    assert tracked[0].price_samples == 3
    assert peak_multiple(tracked[0]) == Decimal("2.5")


async def test_pools_without_prices_stay_out_of_the_report(db):
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope

    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0xquiet",
                                 token_address="0xtoken", status="rejected")

    async with session_scope() as session:
        assert await repo.outcome_pairs(session, "bsc") == []


async def test_reject_codes_survive_a_round_trip(db):
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope

    async with session_scope() as session:
        row = await repo.add_seen_pair(session, chain="bsc", pair_address="0xpool",
                                       token_address="0xtoken", status="new")
        await repo.update_seen_pair(session, row.id, reject_codes="proxy,owner_share")
        await repo.mark_pair(session, row.id, "rejected", "прокси; доля владельца")
        await repo.track_pool_price(session, row.id, Decimal(1))
        await repo.track_pool_price(session, row.id, Decimal(4))

    async with session_scope() as session:
        tracked = await repo.outcome_pairs(session, "bsc")

    outcome = to_outcomes(tracked)[0]
    assert outcome.accepted is False
    assert outcome.codes == ("proxy", "owner_share")
    assert outcome.multiple == Decimal(4)


# ---------------------------------------------------------- подбор под винрейт
def path(peak: str, low: str = "1", accepted: bool = True) -> Outcome:
    """Пул, который сходил вверх до peak× и вниз до low×."""
    return Outcome(accepted=accepted, multiple=Decimal(peak), low=Decimal(low))


def test_simulate_counts_clean_wins_and_losses():
    from sniperbot.pairstats import simulate

    rows = [path("1.5", "0.95"), path("1.02", "0.6"), path("1.05", "0.95")]
    result = simulate(rows, take_profit=30, stop_loss=25, cost_pct=Decimal(5))

    assert (result.wins, result.losses, result.flat, result.ambiguous) == (1, 1, 1, 0)


def test_simulate_marks_ambiguous_when_both_levels_were_touched():
    """Порядок пика и минимума неизвестен — выдавать одно число было бы враньём."""
    from sniperbot.pairstats import simulate

    result = simulate([path("2.0", "0.5")], take_profit=50, stop_loss=30, cost_pct=Decimal(5))

    assert result.ambiguous == 1
    assert result.winrate_low == Decimal(0)
    assert result.winrate_high == Decimal(100)


def test_expectancy_subtracts_costs_from_every_trade():
    from sniperbot.pairstats import simulate

    # Одна чистая победа по TP +50% при издержках 10% — это +40%, а не +50%.
    result = simulate([path("1.6", "0.99")], take_profit=50, stop_loss=30, cost_pct=Decimal(10))
    assert result.expectancy_low == Decimal(40)

    # Сделка, не дошедшая никуда, всё равно стоит издержек.
    flat = simulate([path("1.05", "0.99")], take_profit=50, stop_loss=30, cost_pct=Decimal(10))
    assert flat.expectancy_low == Decimal(-10)


def test_high_winrate_can_still_lose_money():
    """Главная ловушка вопроса «как поднять винрейт»: 75% плюсовых и минус в итоге."""
    from sniperbot.pairstats import simulate

    rows = [path("1.3", "0.95")] * 3 + [path("1.0", "0.4")]
    result = simulate(rows, take_profit=20, stop_loss=50, cost_pct=Decimal(8))

    assert result.winrate_low == Decimal(75)
    assert result.expectancy_low < 0


def test_breakeven_take_profit_matches_hand_arithmetic():
    from sniperbot.pairstats import breakeven_take_profit

    # 45% побед, стоп −25%, издержки 8%: (0.55/0.45)×33 + 8 ≈ 48.3%
    need = breakeven_take_profit(Decimal(45), stop_loss=25, cost_pct=Decimal(8))
    assert Decimal(48) < need < Decimal(49)

    # Чем ниже винрейт, тем крупнее должна быть победа.
    assert breakeven_take_profit(Decimal(20), 25, Decimal(8)) > need


def test_grid_only_returns_combinations_reaching_the_target():
    from sniperbot.pairstats import winrate_grid

    rows = [path("1.25", "0.9")] * 40 + [path("1.0", "0.5")] * 60
    grid = winrate_grid(rows, target=Decimal(35), cost_pct=Decimal(5))

    assert grid, "должны найтись подходящие пары"
    assert all(result.winrate_high >= 35 for result in grid)


def test_winrate_report_states_required_take_profit():
    from sniperbot.pairstats import render_winrate

    rows = [path("1.3", "0.9")] * 45 + [path("1.0", "0.6")] * 55
    text = render_winrate(rows, Decimal(45), Decimal(8))

    assert "45%" in text
    assert "тейк не ниже" in text          # арифметика безубытка на месте
    assert "вилка" in text.lower()          # и оговорка про неизвестный порядок


def test_winrate_report_refuses_to_guess_on_thin_data():
    from sniperbot.pairstats import render_winrate

    text = render_winrate([path("1.3")] * 5, Decimal(45), Decimal(8))
    assert "Данных мало" in text


def test_winrate_report_says_when_target_is_unreachable():
    from sniperbot.pairstats import render_winrate

    rows = [path("1.01", "0.3")] * 60      # ничего не растёт
    text = render_winrate(rows, Decimal(45), Decimal(8))
    assert "недостижимы" in text
