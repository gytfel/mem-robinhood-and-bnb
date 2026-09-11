"""Дописывание новых настроек в .env: ничего чужого не трогаем."""

from __future__ import annotations

from sniperbot.envsync import entries_of, keys_of, merge, missing

EXAMPLE = """BOT_TOKEN=
MASTER_KEY=

# --- Сети ---
BSC_RPC_URLS=
# Uniswap V3 в BSC
#   V3_ROUTER ← SwapRouter02
BSC_V3_ROUTER=
BSC_V3_FACTORY=
# BSC_V3_FEES=100,500        # необязательная, закомментирована
DEPOSIT_FEE_BPS=200
"""


def test_only_uncommented_keys_count_as_present():
    """Закомментированная строка — это выключенная настройка, а не заданная."""
    text = "A=1\n# B=2\n  C = 3\n"
    assert keys_of(text) == {"A", "C"}


def test_entry_carries_the_comments_above_it():
    entries = {entry.key: entry for entry in entries_of(EXAMPLE)}
    assert entries["BSC_V3_ROUTER"].comments == ("# Uniswap V3 в BSC", "#   V3_ROUTER ← SwapRouter02")
    assert entries["BSC_RPC_URLS"].comments == ("# --- Сети ---",)


def test_blank_line_breaks_the_link_to_a_comment():
    """Иначе к настройке прицепится заголовок раздела сверху."""
    entries = {entry.key: entry for entry in entries_of("# шапка\n\nA=1\n")}
    assert entries["A"].comments == ()


def test_existing_values_are_never_touched():
    current = "BOT_TOKEN=123:ABC\nMASTER_KEY=secret\nBSC_RPC_URLS=https://mine\n"
    merged, added = merge(EXAMPLE, current)

    assert merged.startswith(current)          # старое содержимое слово в слово
    assert "BOT_TOKEN=123:ABC" in merged
    assert "BOT_TOKEN=\n" not in merged        # пустой из примера не подмешался
    assert "MASTER_KEY=secret" in merged
    assert added == ["BSC_V3_ROUTER", "BSC_V3_FACTORY", "DEPOSIT_FEE_BPS"]


def test_missing_keys_arrive_with_their_comments():
    merged, _ = merge(EXAMPLE, "BOT_TOKEN=x\nMASTER_KEY=y\nBSC_RPC_URLS=z\n")
    assert "#   V3_ROUTER ← SwapRouter02" in merged
    assert "BSC_V3_ROUTER=" in merged


def test_an_empty_value_still_counts_as_present():
    """Человек мог намеренно оставить настройку пустой — не навязываемся."""
    current = "BOT_TOKEN=\nMASTER_KEY=\nBSC_RPC_URLS=\nBSC_V3_ROUTER=\n"
    _, added = merge(EXAMPLE, current)
    assert "BSC_V3_ROUTER" not in added


def test_running_twice_changes_nothing():
    once, added = merge(EXAMPLE, "BOT_TOKEN=x\n")
    twice, added_again = merge(EXAMPLE, once)

    assert added and added_again == []
    assert twice == once


def test_nothing_to_add_returns_the_file_untouched():
    current = EXAMPLE + "EXTRA=mine\n"
    merged, added = merge(EXAMPLE, current)
    assert added == [] and merged == current


def test_file_without_trailing_newline_is_not_mangled():
    merged, _ = merge(EXAMPLE, "BOT_TOKEN=x")
    assert merged.startswith("BOT_TOKEN=x\n")


def test_commented_optional_settings_are_not_demanded():
    """BSC_V3_FEES закомментирована в примере — требовать её нельзя."""
    assert "BSC_V3_FEES" not in [entry.key for entry in missing(EXAMPLE, "")]


def test_real_example_file_is_parsed():
    """Проверка на настоящем .env.example, а не только на выдуманном."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / ".env.example"
    keys = [entry.key for entry in entries_of(example.read_text(encoding="utf-8"))]

    assert "BOT_TOKEN" in keys
    assert "BSC_V3_ROUTER" in keys and "BSC_V3_QUOTER" in keys
    assert "RH_V3_ROUTER" in keys
    assert len(keys) == len(set(keys)), "в примере есть повторяющиеся ключи"
