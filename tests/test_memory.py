"""Память бота: кеши с потолком и строка в /health.

Бота уже убивала система за нехватку памяти. Проверять здесь надо две вещи:
что кеши, пополняемые с каждым новым токеном, не растут бесконечно, и что
владелец видит память из Telegram, не заходя на сервер.
"""

from __future__ import annotations

from sniperbot.utils.bounded import remember
from sniperbot.utils.memory import health_line, process_mb, server_mb


# ----------------------------------------------------------- кеши с потолком
def test_a_cache_never_grows_past_its_limit():
    cache: dict[int, int] = {}
    for key in range(10_000):
        remember(cache, key, key, 100)
    assert len(cache) == 100


def test_the_oldest_entries_go_first():
    cache: dict[str, int] = {}
    for key in "abcde":
        remember(cache, key, 1, 3)
    assert list(cache) == ["c", "d", "e"]


def test_a_refreshed_entry_is_not_the_first_to_go():
    """Токен в игре, которого увидели давно, не должен вылететь первым."""
    cache: dict[str, int] = {}
    for key in "abc":
        remember(cache, key, 1, 3)
    remember(cache, "a", 2, 3)       # «a» снова нужен
    remember(cache, "d", 1, 3)
    assert "a" in cache and "b" not in cache
    assert cache["a"] == 2


def test_token_details_are_kept_within_a_limit():
    from sniperbot.chain import erc20

    assert erc20.CACHE_LIMIT > 0
    erc20.clear_cache()
    try:
        for index in range(erc20.CACHE_LIMIT + 500):
            remember(erc20._cache, ("rh", f"0x{index:040x}"), (0.0, None), erc20.CACHE_LIMIT)
        assert len(erc20._cache) == erc20.CACHE_LIMIT
    finally:
        erc20.clear_cache()


def test_the_limits_cover_what_is_alive_at_once():
    """Предел меньше живого набора — это не экономия, а лишние запросы к узлу."""
    from sniperbot.chain.erc20 import CACHE_LIMIT
    from sniperbot.sniper.hunter import CHECKED_LIMIT, MAX_CANDIDATES
    from sniperbot.sniper.safety import SLOT_CACHE_LIMIT

    assert CACHE_LIMIT >= 1000
    assert SLOT_CACHE_LIMIT >= 1000
    assert CHECKED_LIMIT >= MAX_CANDIDATES * 100


# ------------------------------------------------------------ строка в /health
STATUS = "Name:\tsniper\nVmPeak:\t 969844 kB\nVmRSS:\t  250968 kB\nThreads:\t4\n"
MEMINFO = ("MemTotal:         912384 kB\nMemFree:          102400 kB\n"
           "MemAvailable:     153600 kB\nSwapTotal:             0 kB\n")


def test_the_bot_memory_is_read_from_the_system():
    assert process_mb(STATUS) == 245


def test_the_server_memory_is_read_from_the_system():
    assert server_mb(MEMINFO) == (150, 891, 0)


def test_the_health_line_says_what_matters():
    line = health_line(STATUS, MEMINFO)
    assert "бот 245 МБ" in line
    assert "свободно на сервере 150 из 891 МБ" in line
    assert "подкачки нет" in line, "без подкачки запаса нет совсем — об этом надо сказать"


def test_a_server_on_the_edge_is_flagged():
    tight = MEMINFO.replace("153600", "40960")      # 40 МБ из 891
    assert health_line(STATUS, tight).startswith("⚠️")
    assert not health_line(STATUS, MEMINFO).startswith("⚠️")


def test_with_swap_the_line_does_not_complain_about_it():
    with_swap = MEMINFO.replace("SwapTotal:             0 kB", "SwapTotal:       1048576 kB")
    assert "подкачки нет" not in health_line(STATUS, with_swap)


def test_without_system_details_the_line_is_simply_absent():
    """Не Linux или нет доступа к /proc — строку не показываем, а не ломаем /health."""
    assert health_line("", "") == ""
