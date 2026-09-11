"""Реестр настроек: разбор значений, области хранения, целостность."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniperbot.db.models import ChainSettings, User
from sniperbot.settings_registry import (
    SETTINGS,
    apply_value,
    by_group,
    effective_gas_multiplier,
    find,
)


def make_cfg() -> ChainSettings:
    return ChainSettings(user_id=1, chain="bsc")


def test_every_setting_maps_to_a_real_model_field():
    """Опечатка в имени поля сломала бы сохранение — ловим её здесь."""
    for setting in SETTINGS:
        model = User if setting.scope == "user" else ChainSettings
        assert hasattr(model, setting.field), f"{setting.name} -> {model.__name__}.{setting.field}"


def test_setting_names_are_unique_and_lowercase():
    names = [s.name for s in SETTINGS]
    assert len(names) == len(set(names))
    assert all(name == name.lower() and " " not in name for name in names)


def test_percent_settings_store_basis_points():
    assert find("slippage").parse("25") == 2500
    assert find("buytax").parse("7.5") == 750


def test_multiplier_setting_stores_bps():
    assert find("gas").parse("1.5") == 15_000


def test_bool_setting_accepts_russian_and_english():
    autosell = find("autosell")
    assert autosell.parse("on") is True
    assert autosell.parse("выкл") is False
    with pytest.raises(ValueError, match="on или off"):
        autosell.parse("возможно")


def test_choice_setting_validates_options():
    assert find("route").parse("V3") == "v3"
    with pytest.raises(ValueError, match="допустимо"):
        find("route").parse("v9")


def test_numeric_bounds_are_enforced():
    with pytest.raises(ValueError, match="максимум"):
        find("sl").parse("150")
    with pytest.raises(ValueError, match="минимум"):
        find("buy").parse("0")
    with pytest.raises(ValueError, match="нужно число"):
        find("buy").parse("много")


def test_apply_writes_to_the_right_object():
    cfg, user = make_cfg(), User(id=1)

    apply_value("tp", "250", cfg, user)
    assert cfg.take_profit_pct == 250

    apply_value("dry", "on", cfg, user)          # scope=user
    assert user.dry_run is True
    assert not hasattr(cfg, "dry_run") or getattr(cfg, "dry_run", None) is None


def test_unknown_setting_is_rejected():
    with pytest.raises(ValueError, match="неизвестная настройка"):
        apply_value("нетакой", "1", make_cfg(), User(id=1))


def test_display_formats_values_for_humans():
    cfg, user = make_cfg(), User(id=1)
    cfg.slippage_bps = 1500
    cfg.gas_multiplier_bps = 12_000
    cfg.buy_amount = Decimal("0.05")
    cfg.auto_sell = True
    assert find("slippage").display(cfg, user) == "15%"
    assert find("gas").display(cfg, user) == "×1.2"
    assert find("buy").display(cfg, user, "BNB") == "0.05 BNB"
    assert find("autosell").display(cfg, user) == "вкл"
    # значение ещё не задано (в БД его подставит default) — показываем прочерк
    assert find("cooldown").display(cfg, user) == "—"


def test_gas_mode_overrides_manual_multiplier():
    cfg = make_cfg()
    cfg.gas_multiplier_bps = 12_000

    cfg.gas_mode = "manual"
    assert effective_gas_multiplier(cfg) == Decimal("1.2")

    cfg.gas_mode = "turbo"
    assert effective_gas_multiplier(cfg) == Decimal("2.5")

    cfg.gas_mode = "fast"
    assert effective_gas_multiplier(cfg) == Decimal("1.5")


def test_groups_cover_all_settings():
    grouped = by_group()
    assert sum(len(items) for items in grouped.values()) == len(SETTINGS)
    assert {"trade", "exits", "filters", "risk", "ux"} <= set(grouped)


# ------------------------------------------------------ показ настроек в боте
def filled_cfg() -> tuple[ChainSettings, User]:
    from decimal import Decimal as D

    cfg, user = make_cfg(), User(id=1)
    for setting in SETTINGS:
        if setting.kind == "bool":
            setting.write(True, cfg, user)
        elif setting.kind == "choice":
            setting.write(setting.choices[0], cfg, user)
        elif setting.kind == "ladder":
            setting.write("100:50", cfg, user)
        elif setting.kind == "decimal":
            setting.write(D("0.01"), cfg, user)
        else:
            setting.write(50, cfg, user)
    return cfg, user


def test_compact_view_shows_every_setting_and_fits_one_message():
    from sniperbot.bot.ui import TELEGRAM_LIMIT
    from sniperbot.settings_registry import render_compact

    cfg, user = filled_cfg()
    text = render_compact(cfg, user, "BNB")

    assert len(text) < TELEGRAM_LIMIT           # краткий экран влезает целиком
    for setting in SETTINGS:
        assert f"<code>{setting.name}</code>" in text


def test_full_view_includes_hints():
    from sniperbot.settings_registry import render_full

    cfg, user = filled_cfg()
    text = render_full(cfg, user, "BNB")
    for setting in SETTINGS:
        assert setting.title in text
        if setting.hint:
            assert setting.hint.split(".")[0][:40] in text


def test_group_view_shows_only_its_group():
    from sniperbot.settings_registry import render_full

    cfg, user = filled_cfg()
    text = render_full(cfg, user, "BNB", group="filters")
    assert "<code>minliq</code>" in text
    assert "<code>tp</code>" not in text        # выходы сюда не попадают


def test_single_setting_card_explains_limits():
    from sniperbot.settings_registry import find, render_one

    cfg, user = filled_cfg()
    card = render_one(find("sl"), cfg, user, "BNB")
    assert "Стоп-лосс" in card
    assert "Сейчас:" in card
    assert "0–99" in card
    assert "/set sl" in card


def test_ladder_card_shows_example_and_off_switch():
    from sniperbot.settings_registry import find, render_one

    cfg, user = filled_cfg()
    card = render_one(find("ladder"), cfg, user, "BNB")
    assert "[[1.5, 40]" in card        # запись множителями — основная
    assert "50:40,200:30" in card      # и та же лестница в процентах роста
    assert "off" in card


def test_user_scoped_setting_says_it_is_global():
    from sniperbot.settings_registry import find, render_one

    cfg, user = filled_cfg()
    assert "общая для всех сетей" in render_one(find("dry"), cfg, user, "BNB")
    assert "своя для каждой сети" in render_one(find("buy"), cfg, user, "BNB")


# ------------------------------------------------------------------- пресеты
def test_presets_only_use_real_settings_with_valid_values():
    """Пресет не должен уметь записать то, что вручную записать нельзя."""
    from sniperbot.settings_registry import BY_NAME, PRESETS

    for preset in PRESETS:
        for name, raw in preset.values.items():
            setting = BY_NAME.get(name)
            assert setting is not None, f"{preset.name}: настройки «{name}» нет"
            assert setting.scope == "chain", f"{preset.name}: «{name}» не настройка сети"
            setting.parse(raw)      # бросит ValueError, если значение вне диапазона


def test_preset_changes_skip_values_already_set():
    from sniperbot.db.models import ChainSettings
    from sniperbot.settings_registry import PRESETS_BY_NAME, preset_changes

    preset = PRESETS_BY_NAME["momentum"]
    cfg = ChainSettings(user_id=1, chain="bsc")
    for setting, value, _ in preset_changes(preset, cfg):
        setting.write(value, cfg)

    assert preset_changes(preset, cfg) == []      # повторное применение ничего не меняет
    assert cfg.momentum_enabled is True
    assert cfg.stop_loss_pct == 30


def test_presets_keep_exit_rules_consistent():
    """Трейлинг не должен срабатывать раньше первой ступени фиксации."""
    from sniperbot.settings_registry import PRESETS, ladder_steps, parse_ladder

    for preset in PRESETS:
        steps = ladder_steps(parse_ladder(preset.values.get("ladder", "")))
        trail = int(preset.values.get("trail", 0))
        assert steps, f"{preset.name}: без лестницы прибыль не фиксируется"
        first_step = steps[0][0]
        assert trail < first_step, f"{preset.name}: трейлинг {trail}% съест ступень +{first_step}%"


# --------------------------------------------------------------- часы торговли
def test_hours_parsing_accepts_ranges_lists_and_midnight_crossing():
    from sniperbot.settings_registry import parse_hours

    assert parse_hours("00-03") == "00,01,02,03"
    assert parse_hours("14,16,21") == "14,16,21"
    assert parse_hours("22-2") == "00,01,02,22,23"      # окно через полночь
    assert parse_hours("off") == ""
    assert parse_hours("0-23") == ""                    # все часы = ограничения нет


def test_hours_parsing_rejects_nonsense():
    import pytest

    from sniperbot.settings_registry import parse_hours

    for raw in ("25", "abc", "1-99", "10:00"):
        with pytest.raises(ValueError):
            parse_hours(raw)


def test_hours_are_shown_as_readable_spans():
    from sniperbot.settings_registry import format_hours

    assert format_hours("00,01,02,14") == "00-02, 14 UTC"
    assert format_hours("") == "круглосуточно"


def test_trading_allowed_respects_the_window():
    import datetime as dt

    from sniperbot.settings_registry import trading_allowed

    inside = dt.datetime(2026, 9, 9, 21, tzinfo=dt.UTC)
    outside = dt.datetime(2026, 9, 9, 11, tzinfo=dt.UTC)

    assert trading_allowed("20,21,22", inside) is True
    assert trading_allowed("20,21,22", outside) is False
    assert trading_allowed("", outside) is True        # пусто — круглосуточно


# --------------------------------------------------------- лестница фиксаций
def test_ladder_reads_the_multiplier_notation():
    """Ровно та запись, которую человек пишет руками: [[множитель, доля], …]."""
    from sniperbot.settings_registry import parse_ladder

    assert parse_ladder("[[1.5, 40], [3, 30], [10, 30]]") == "50:40,200:30,900:30"
    assert parse_ladder("[[1.5,40],[3,30]]") == "50:40,200:30"
    assert parse_ladder("1.5x:40, 3x:30") == "50:40,200:30"
    assert parse_ladder("×2:50") == "100:50"


def test_ladder_still_reads_growth_in_percent():
    """Старые значения и старые привычки продолжают работать."""
    from sniperbot.settings_registry import parse_ladder

    assert parse_ladder("100:50,300:30") == "100:50,300:30"
    assert parse_ladder("50%:40") == "50:40"


def test_ladder_steps_are_sorted_by_growth():
    from sniperbot.settings_registry import parse_ladder

    assert parse_ladder("[[3, 30], [1.5, 40]]") == "50:40,200:30"


def test_a_multiplier_written_as_percent_is_caught():
    """1.5:40 — это почти наверняка ×1.5, а не ступень в полутора процентах."""
    from sniperbot.settings_registry import parse_ladder

    with pytest.raises(ValueError, match="1.5x"):
        parse_ladder("1.5:40")


def test_multiplier_below_one_is_rejected():
    from sniperbot.settings_registry import parse_ladder

    with pytest.raises(ValueError, match="больше 1"):
        parse_ladder("[[0.9, 40]]")


def test_broken_ladder_input_is_explained():
    from sniperbot.settings_registry import parse_ladder

    with pytest.raises(ValueError, match=r"\[\[1.5, 40\]"):
        parse_ladder("[[1.5, 40], [3]]")
    with pytest.raises(ValueError, match="больше 100"):
        parse_ladder("[[1.5, 60], [3, 60]]")
    with pytest.raises(ValueError, match="от 1 до 100"):
        parse_ladder("[[1.5, 0]]")


def test_ladder_switches_off():
    from sniperbot.settings_registry import parse_ladder

    for text in ("", "off", "выкл", "[]"):
        assert parse_ladder(text) == ""


def test_ladder_is_shown_as_multipliers():
    from sniperbot.settings_registry import format_ladder

    assert format_ladder("50:40,200:30,900:30") == "×1.5 → 40% · ×3 → 30% · ×10 → 30%"
    assert format_ladder("") == "выключена"


def test_ladder_note_spells_out_the_growth_and_the_remainder():
    """Чтобы не гадать, что бот понял из [[1.5, 40], [3, 30]]."""
    from sniperbot.settings_registry import ladder_note

    note = ladder_note("50:40,200:30")
    assert "+50% → 40%" in note and "+200% → 30%" in note
    assert "остальные 30%" in note

    assert "целиком" in ladder_note("50:40,200:30,900:30")


def test_ladder_note_warns_which_steps_the_secure_sale_will_take():
    """Иначе остаётся вопрос, почему ступень ×3 так и не сработала."""
    from sniperbot.settings_registry import ladder_note

    note = ladder_note("50:40,200:30,900:30", secure_pct=40)
    assert "71% позиции" in note              # 1/1.4 — столько вернёт вложенное
    assert "×1.5, ×3" in note                 # эти ступени возврат закрывает
    assert "×10" not in note.split("закроет ступени")[1].split("—")[0]
    assert "/set secure 0" in note

    # Высокий порог возврата продаёт мало и лестницу не трогает.
    high = ladder_note("50:40,200:30,900:30", secure_pct=400)
    assert "закроет ступени" not in high
