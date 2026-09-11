"""Правила выхода из позиции: приоритеты, лестница, безубыток, защита от слива."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniperbot.db.models import Position
from sniperbot.sniper.positions import (
    ExitContext,
    decide_exit,
    ladder_percent,
    retired_steps,
    secure_share,
)
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
        liquidity=None, peak_liquidity=None, age_minutes=0.0, exit_cost=0) -> ExitContext:
    change = Decimal(str(change))
    price = Decimal(str(price)) if price is not None else Decimal(1) + change / 100
    peak_change = Decimal(str(peak_change)) if peak_change is not None else max(change, Decimal(0))
    peak_price = Decimal(str(peak_price)) if peak_price is not None else Decimal(1) + peak_change / 100
    return ExitContext(
        change=change, peak_change=peak_change, price=price, peak_price=peak_price,
        liquidity=Decimal(str(liquidity)) if liquidity is not None else None,
        peak_liquidity=Decimal(str(peak_liquidity)) if peak_liquidity is not None else None,
        age_minutes=age_minutes,
        exit_cost=Decimal(str(exit_cost)),
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


# ------------------------------------------ взаимодействие лестницы и трейлинга
def test_trailing_guards_the_remainder_after_ladder_steps():
    """После ступеней остаток защищён только входом — трейлинг закрывает разрыв."""
    pos = position(tp_ladder="50:40,150:30", tp_done="50,150", take_profit_pct=0,
                   trailing_stop_pct=40, breakeven_armed=True,
                   amount_wei=to_wei(30), bought_wei=to_wei(100))

    # цена ушла на +500% и откатилась до +100%: откат 66% от максимума
    rule, percent, _ = decide_exit(pos, ctx(100, peak_change=500, price=2.0, peak_price=6.0))
    assert rule.key == "trailing"
    assert percent == 100


def test_tight_trailing_fires_on_a_dip_between_ladder_steps():
    """Узкий трейлинг закрывает позицию на обычном провале — это цена тесной настройки."""
    pos = position(tp_ladder="50:40,150:30,400:20", tp_done="50", take_profit_pct=0,
                   trailing_stop_pct=25, breakeven_armed=True)

    # после первой ступени цена просела с +50% до +8%: откат 28% > 25%
    tight, _, _ = decide_exit(pos, ctx(8, peak_change=50, price=1.08, peak_price=1.5))
    assert tight.key == "trailing"

    # тот же провал при широком трейлинге позицию не трогает
    pos.trailing_stop_pct = 40
    wide, _, _ = decide_exit(pos, ctx(8, peak_change=50, price=1.08, peak_price=1.5))
    assert wide is None


def test_trailing_never_touches_a_losing_position():
    """В минусе трейлинг молчит: там работает стоп-лосс."""
    pos = position(take_profit_pct=0, stop_loss_pct=0, trailing_stop_pct=20)
    assert decide_exit(pos, ctx(-50, peak_change=80, price=0.5, peak_price=1.8))[0] is None


# ------------------------------------------------ обвал против обычного стопа
def test_collapse_is_not_reported_as_a_plain_stop_loss():
    """−100% при стопе −30% выглядит поломкой стопа, хотя это вынутая ликвидность."""
    from sniperbot.sniper.positions import RULE_COLLAPSE

    rule, percent, _ = decide_exit(position(stop_loss_pct=30), ctx(-100))

    assert rule is RULE_COLLAPSE
    assert percent == 100


def test_ordinary_stop_keeps_its_name():
    from sniperbot.sniper.positions import RULE_STOP

    rule, _, _ = decide_exit(position(stop_loss_pct=30), ctx(-35))
    assert rule is RULE_STOP


def test_a_deep_stop_setting_is_still_a_stop():
    """Если человек сам поставил стоп −95%, обвалом это называть незачем."""
    from sniperbot.sniper.positions import RULE_STOP

    rule, _, _ = decide_exit(position(stop_loss_pct=95), ctx(-96))
    assert rule is RULE_STOP


# --------------------------------------------- защита от слива и сбои сети
def test_rug_guard_needs_a_known_peak_to_compare_with():
    """Без замера ликвидности сравнивать не с чем — правило обязано молчать."""
    rule, _, _ = decide_exit(
        position(stop_loss_pct=0, rug_guard_pct=40),
        ctx(-50, liquidity=0, peak_liquidity=0),
    )
    assert rule is None


def test_rug_guard_fires_when_liquidity_leaves():
    from sniperbot.sniper.positions import RULE_RUG

    rule, percent, _ = decide_exit(
        position(stop_loss_pct=0, rug_guard_pct=40),
        ctx(-20, liquidity=3, peak_liquidity=10),
    )
    assert rule is RULE_RUG and percent == 100


def test_unknown_liquidity_never_triggers_a_rug_exit():
    """Сбой RPC не должен продавать живую позицию — это отдельная беда."""
    rule, _, _ = decide_exit(
        position(stop_loss_pct=0, rug_guard_pct=40, take_profit_pct=0),
        ctx(15, liquidity=None, peak_liquidity=10),
    )
    assert rule is None


# ------------------------------------------------------- возврат вложенного
def secured(**kwargs):
    """Позиция на 100 токенов, купленных за 100 монет: цена входа — единица."""
    defaults = {"native_spent_wei": to_wei(100), "native_returned_wei": 0,
                "token_decimals": 18, "secure_pct": 40, "take_profit_pct": 0,
                "stop_loss_pct": 0}
    defaults.update(kwargs)
    return position(**defaults)


def test_secure_sells_exactly_what_returns_the_stake():
    """Ступень «40% на +50%» вернула бы 60% вложенного; здесь возвращается всё."""
    rule, percent, marker = decide_exit(secured(), ctx(50))

    assert rule.key == "secure" and marker == "secure"
    assert percent == 67                      # 100 из 150 = 2/3 остатка
    # Проверка смысла: проданная доля по текущей цене покрывает вложенное.
    assert Decimal(percent) / 100 * Decimal("1.5") * 100 >= 100


def test_secure_includes_the_cost_of_its_own_sale():
    """Газ — не мелочь при малом входе: вернуть «ровно вложенное» его не покроет."""
    without = decide_exit(secured(), ctx(50))[1]
    with_gas = decide_exit(secured(), ctx(50, exit_cost=15))[1]
    assert with_gas > without == 67
    assert with_gas == 77                     # (100 + 15) из 150


def test_secure_waits_until_growth_covers_the_stake():
    """На +5% пришлось бы продать почти всё — это уже не частичная фиксация."""
    rule, _, _ = decide_exit(secured(secure_pct=5), ctx(5))
    assert rule is None


def test_secure_counts_money_already_returned():
    """После ступени лестницы вернуть нужно только остаток долга."""
    rule, percent, _ = decide_exit(secured(native_returned_wei=to_wei(60)), ctx(50))
    assert rule.key == "secure"
    assert percent == 27                      # 40 из 150


def test_secure_happens_once():
    assert decide_exit(secured(tp_done="secure"), ctx(80))[0] is None


def test_secure_goes_before_the_ladder():
    """Сначала сделка перестаёт быть убыточной, фиксация прибыли — потом."""
    rule, _, _ = decide_exit(secured(tp_ladder="50:40"), ctx(50))
    assert rule.key == "secure"


def test_secure_retires_the_steps_it_already_sold_for():
    """Иначе следующая проверка продаст ступенью тот самый оставленный хвост."""
    pos = secured(tp_ladder="50:40,150:30")

    # Продали 72% — это больше, чем обе ступени вместе (40 + 30).
    assert retired_steps(pos, 72) == ["secure", "50", "150"]
    # Продали 50% — первой ступени хватило, вторая ещё своё возьмёт.
    assert retired_steps(pos, 50) == ["secure", "50"]
    assert retired_steps(pos, 30) == ["secure"]


def test_retired_steps_count_from_the_original_size():
    """Доля ступени считается от исходного объёма — значит и проданное тоже."""
    # Половина позиции уже продана раньше; 80% остатка — это 40% исходного.
    pos = secured(tp_ladder="50:40,150:30", amount_wei=to_wei(50), bought_wei=to_wei(100))
    assert retired_steps(pos, 80) == ["secure", "50"]
    assert retired_steps(pos, 50) == ["secure"]


def test_saving_money_never_outranks_saving_the_position():
    """Стоп и слив ликвидности важнее: возврат вложенного — про прибыль."""
    assert decide_exit(secured(stop_loss_pct=30), ctx(-40))[0].key == "stop_loss"
    assert decide_exit(secured(rug_guard_pct=25),
                       ctx(50, liquidity=1, peak_liquidity=10))[0].key == "rug"


def test_secure_is_off_by_default_for_old_positions():
    """У позиций, открытых до обновления, поля нет — правило молчит."""
    assert decide_exit(position(secure_pct=None, take_profit_pct=0), ctx(80))[0] is None


@pytest.mark.parametrize("need,value,expected", [
    (Decimal(100), Decimal(200), 50),      # рост вдвое — половина остатка
    (Decimal(100), Decimal(150), 67),      # +50% — две трети
    (Decimal(100), Decimal(101), 0),       # почти вся позиция: не наш случай
    (Decimal(0), Decimal(150), 0),         # возвращать нечего
    (Decimal(100), Decimal(0), 0),         # остаток ничего не стоит
])
def test_secure_share_arithmetic(need, value, expected):
    assert secure_share(need, value) == expected


def walk(pos, prices) -> list[tuple[str, int]]:
    """Прогоняет позицию по ценам так, как это делает монитор."""
    trades = []
    for change in prices:
        for _ in range(4):        # за одну проверку срабатывает одно правило
            rule, percent, marker = decide_exit(pos, ctx(change))
            if rule is None:
                break
            # Порядок как в мониторе: метки считаются от объёма до продажи.
            markers = retired_steps(pos, percent) if rule.key == "secure" else (
                [marker] if marker else [])
            sold = pos.amount_wei * percent // 100
            price = Decimal(1) + Decimal(change) / 100
            pos.native_returned_wei += int(sold * price)
            pos.amount_wei -= sold
            done = [step for step in (pos.tp_done or "").split(",") if step]
            pos.tp_done = ",".join(done + [m for m in markers if m not in done])
            if markers:
                pos.breakeven_armed = True      # так делает _mark_ladder_step
            trades.append((rule.key, percent))
            if pos.amount_wei <= 0:
                return trades
    return trades


def test_the_runner_survives_the_steps_the_secure_sale_covered():
    """Главная ловушка: ступень «40% от исходного» снесла бы хвост целиком."""
    pos = secured(tp_ladder="50:40", trailing_stop_pct=0, native_spent_wei=to_wei(100))
    trades = walk(pos, [45, 60, 200, 400])

    assert trades == [("secure", 69)]               # ступень погашена возвратом
    assert pos.amount_wei > 0                       # хвост едет дальше
    assert pos.native_returned_wei >= to_wei(100)   # вложенное уже на кошельке


def test_a_step_the_secure_sale_did_not_cover_still_takes_its_profit():
    """Возврат продал 69% исходного объёма, обе ступени просят 70% — вторая жива."""
    pos = secured(tp_ladder="50:40,150:30", trailing_stop_pct=0, native_spent_wei=to_wei(100))
    trades = walk(pos, [45, 60, 200])

    assert [rule for rule, _ in trades] == ["secure", "ladder"]
    assert pos.native_returned_wei > to_wei(150)    # вложенное плюс прибыль ступени


def test_secure_on_a_big_jump_covers_the_whole_ladder():
    """Чем выше рост, тем меньше доля возврата — но ступени она всё равно закрывает."""
    pos = secured(secure_pct=100, tp_ladder="150:30", trailing_stop_pct=0,
                  native_spent_wei=to_wei(100))
    trades = walk(pos, [120, 200])

    assert [rule for rule, _ in trades] == ["secure"]
    assert pos.amount_wei > 0


# --------------------------------------------------- тейк-профит частями
def test_partial_take_profit_fires_once_not_every_check():
    """Иначе доля 40% съедала бы позицию целиком за несколько секунд опроса."""
    pos = position(take_profit_pct=300, sell_percent=40, trailing_stop_pct=0,
                   secure_pct=0, native_spent_wei=to_wei(100),
                   native_returned_wei=0, token_decimals=18)
    trades = walk(pos, [320, 350, 400])

    assert trades == [("take_profit", 40)]
    assert pos.amount_wei > 0                 # остаток едет дальше
    assert "tp" in pos.tp_done


def test_a_full_take_profit_closes_the_position():
    pos = position(take_profit_pct=300, sell_percent=100, trailing_stop_pct=0,
                   secure_pct=0, native_spent_wei=to_wei(100),
                   native_returned_wei=0, token_decimals=18)
    assert walk(pos, [320]) == [("take_profit", 100)]
    assert pos.amount_wei == 0


def test_the_remainder_is_protected_after_a_partial_take():
    """После фиксации прибыль уже снята — стоп переносится в безубыток."""
    pos = position(take_profit_pct=300, sell_percent=40, trailing_stop_pct=0,
                   secure_pct=0, native_spent_wei=to_wei(100),
                   native_returned_wei=0, token_decimals=18)
    walk(pos, [320])
    assert pos.breakeven_armed is True

    rule, percent, _ = decide_exit(pos, ctx(-1))
    assert rule.key == "breakeven" and percent == 100


def test_take_profit_in_multiples_means_the_same_thing():
    from sniperbot.settings_registry import find, parse_growth

    assert parse_growth("4x") == 300
    assert parse_growth("×2") == 100
    assert parse_growth("300") == 300          # голое число остаётся процентами
    assert find("tp").parse("4x") == 300
    assert find("tp").parse("300") == 300


def test_a_multiplier_below_one_is_refused():
    from sniperbot.settings_registry import find

    with pytest.raises(ValueError, match="больше 1"):
        find("tp").parse("0.5x")


def test_growth_settings_show_both_notations():
    from sniperbot.db.models import ChainSettings
    from sniperbot.settings_registry import find

    cfg = ChainSettings(user_id=1, chain="bsc", secure_pct=50)
    assert find("secure").display(cfg) == "+50% (×1.5)"

    cfg = ChainSettings(user_id=1, chain="bsc", take_profit_pct=300, sell_percent=40,
                        tp_ladder="")
    assert find("tp").display(cfg) == "×4 (+300%), продать 40%"


# ------------------------------------- перенос настроек на открытые позиции
def test_apply_copies_the_current_rules_onto_an_open_position():
    """Позиция открывалась со старым тейком — /apply переносит новый."""
    from sniperbot.db.models import ChainSettings
    from sniperbot.settings_registry import find
    from sniperbot.sniper.executor import copy_exit_rules

    cfg = ChainSettings(user_id=1, chain="rh", stop_loss_pct=30, trailing_stop_pct=40,
                        sell_percent=100, auto_sell=True, secure_pct=0, breakeven_pct=0,
                        rug_guard_pct=0, dead_timeout_min=0, dead_min_pct=0)
    find("tp").write(find("tp").parse("[[1.5, 40], [3, 30]]"), cfg)

    pos = position(take_profit_pct=300, sell_percent=100, tp_ladder="")
    changed = copy_exit_rules(cfg, pos)

    assert "tp_ladder" in changed and "take_profit_pct" in changed
    assert pos.tp_ladder == "50:40,200:30"
    assert pos.take_profit_pct == 0

    # Теперь ступень срабатывает там, где раньше позиция просто ехала мимо.
    rule, percent, marker = decide_exit(pos, ctx(65))
    assert rule.key == "ladder" and percent == 40 and marker == "50"


def test_apply_reports_nothing_to_change():
    from sniperbot.db.models import ChainSettings
    from sniperbot.sniper.executor import copy_exit_rules

    cfg = ChainSettings(user_id=1, chain="rh", take_profit_pct=300, stop_loss_pct=50,
                        trailing_stop_pct=0, sell_percent=100, auto_sell=True,
                        tp_ladder="", secure_pct=0, breakeven_pct=0, rug_guard_pct=0,
                        dead_timeout_min=0, dead_min_pct=0)
    pos = position(take_profit_pct=300, stop_loss_pct=50, trailing_stop_pct=0,
                   sell_percent=100, auto_sell=True, tp_ladder="", secure_pct=0,
                   breakeven_pct=0, rug_guard_pct=0, dead_timeout_min=0, dead_min_pct=0)
    assert copy_exit_rules(cfg, pos) == []
