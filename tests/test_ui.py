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
        elif setting.kind == "ladder":
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
