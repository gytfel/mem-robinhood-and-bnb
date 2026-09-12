"""Разбиение длинных сообщений: Telegram отвергает всё длиннее 4096 символов."""

from __future__ import annotations

import pytest

from sniperbot.bot.ui import TELEGRAM_LIMIT, split_message


def test_short_text_is_not_split():
    assert split_message("привет") == ["привет"]


def test_long_text_is_split_within_limit():
    text = "\n".join(f"строка номер {i} с описанием настройки" for i in range(300))
    parts = split_message(text)

    assert len(parts) > 1
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    # содержимое не теряется
    assert "\n".join(parts).replace("\n", "") == text.replace("\n", "")


def test_split_happens_on_line_boundaries():
    """Разрыв внутри строки порвал бы HTML-разметку."""
    line = "<b>жирная строка</b> со значением"
    text = "\n".join([line] * 300)
    for part in split_message(text):
        assert part.startswith("<b>")
        assert part.endswith("</b> со значением")


def test_single_overlong_line_is_cut_hard():
    parts = split_message("a" * 10_000)
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(parts) == "a" * 10_000


def test_config_screen_fits_after_splitting():
    """Полный /config длиннее лимита — именно из-за этого он не доходил."""
    from decimal import Decimal

    from sniperbot.db.models import ChainSettings, User
    from sniperbot.settings_registry import GROUPS, SETTINGS, by_group

    cfg, user = ChainSettings(user_id=1, chain="bsc"), User(id=1)
    for setting in SETTINGS:
        if setting.kind == "bool":
            setting.write(True, cfg, user)
        elif setting.kind == "choice":
            setting.write(setting.choices[0], cfg, user)
        elif setting.kind in {"ladder", "tp"}:
            setting.write("", cfg, user)
        elif setting.kind == "decimal":
            setting.write(Decimal("0.01"), cfg, user)
        else:
            setting.write(100, cfg, user)

    lines = []
    for group, title in GROUPS.items():
        lines.append(f"<b>{title}</b>")
        for setting in by_group()[group]:
            lines.append(f"<code>{setting.name}</code> = <b>{setting.display(cfg, user, 'BNB')}</b>"
                         f"\n    <i>{setting.hint}</i>")
    text = "\n".join(lines)

    assert len(text) > TELEGRAM_LIMIT          # без разбиения сообщение не ушло бы
    parts = split_message(text)
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    for setting in SETTINGS:                   # каждая настройка видна пользователю
        assert any(f"<code>{setting.name}</code>" in part for part in parts)


@pytest.mark.parametrize("size", [4095, 4096, 4097])
def test_boundary_sizes(size):
    parts = split_message("x" * size)
    assert all(len(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(parts) == "x" * size


# ------------------------------------------- правила выхода в карточке позиции
def exit_position(**kwargs):
    from sniperbot.db.models import Position

    defaults = {
        "id": 1, "user_id": 1, "chain": "rh", "token_address": "0x1", "token_symbol": "M",
        "auto_sell": True, "take_profit_pct": 0, "sell_percent": 100, "tp_ladder": "",
        "tp_done": "", "secure_pct": 0, "stop_loss_pct": 30, "trailing_stop_pct": 40,
        "breakeven_armed": False,
    }
    defaults.update(kwargs)
    return Position(**defaults)


def test_card_shows_the_ladder_not_silence():
    """Позиция со ступенями раньше показывала выход вообще без тейка."""
    from sniperbot.bot.views import exit_rules

    text = exit_rules(exit_position(tp_ladder="50:40,200:30,900:30"))
    assert "×1.5→40%" in text and "×3→30%" in text and "×10→30%" in text


def test_card_marks_the_steps_that_already_fired():
    from sniperbot.bot.views import exit_rules

    text = exit_rules(exit_position(tp_ladder="50:40,200:30", tp_done="50"))
    assert "×1.5→40%✅" in text
    assert "×3→30%," in text + ","        # вторая ступень ещё впереди


def test_card_shows_the_single_step_form_too():
    from sniperbot.bot.views import exit_rules

    assert "TP ×4 (продать всё)" in exit_rules(exit_position(take_profit_pct=300))
    assert "продать 40%" in exit_rules(exit_position(take_profit_pct=300, sell_percent=40))


def test_card_shows_stake_recovery_and_breakeven():
    from sniperbot.bot.views import exit_rules

    text = exit_rules(exit_position(secure_pct=40, tp_done="secure", breakeven_armed=True))
    assert "возврат вложенного ×1.4✅" in text
    assert "стоп в безубытке" in text


def test_card_says_when_autosell_is_off():
    from sniperbot.bot.views import exit_rules

    assert exit_rules(exit_position(auto_sell=False)) == "выключен"


def test_card_says_when_there_is_no_take_profit_at_all():
    """Молчание читается как «всё в порядке», хотя фиксации прибыли нет."""
    from sniperbot.bot.views import exit_rules

    text = exit_rules(exit_position(take_profit_pct=0, tp_ladder=""))
    assert "TP не задан" in text
    assert "SL −30%" in text


def test_ab_value_with_spaces_reaches_the_setting():
    """/ab set tp [[1.5, 40], [3, 30]] — это одно значение, а не три слова."""
    typed = "set tp [[1.5, 40], [3, 30]]"      # то, что человек набирает в боте
    parts = typed.split(maxsplit=2)           # ровно так их делит /ab
    assert parts[1] == "tp"
    assert parts[2] == "[[1.5, 40], [3, 30]]"

    from sniperbot.settings_registry import find

    assert find("tp").parse(parts[2]) == "50:40,200:30"


# ----------------------------------------- карточка открывается сразу, без сети
def test_card_renders_from_the_stored_price():
    """Экран не должен ждать ноду: нет цены из сети — показываем последнюю известную."""
    from decimal import Decimal

    from sniperbot.bot.views import render_position
    from sniperbot.config import ChainConfig
    from sniperbot.utils.fmt import to_wei

    chain = ChainConfig(key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH",
                        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40)
    position = exit_position(
        token_address="0x" + "a" * 40, token_decimals=18, amount_wei=to_wei(1000),
        native_spent_wei=to_wei("0.001"), native_returned_wei=0,
        entry_price=Decimal("0.000001"), last_price=Decimal("0.0000015"), status="open",
        ab_group="",
    )

    stale = render_position(position, chain, position.last_price, stale=True)
    assert "из последней проверки" in stale
    assert "P&L" in stale                      # оценка есть сразу, а не после ноды

    fresh = render_position(position, chain, Decimal("0.0000016"))
    assert "из последней проверки" not in fresh


def test_card_without_any_price_still_opens():
    from sniperbot.bot.views import render_position
    from sniperbot.config import ChainConfig
    from sniperbot.utils.fmt import to_wei

    chain = ChainConfig(key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH",
                        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40)
    position = exit_position(token_address="0x" + "a" * 40, token_decimals=18,
                             amount_wei=to_wei(1000), native_spent_wei=to_wei("0.001"),
                             native_returned_wei=0, last_price=None, status="open", ab_group="")

    text = render_position(position, chain, None, stale=True)
    assert "Позиция #1" in text and "Остаток" in text
