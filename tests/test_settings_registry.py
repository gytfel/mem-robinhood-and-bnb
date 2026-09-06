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
