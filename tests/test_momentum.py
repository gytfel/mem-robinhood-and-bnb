"""Перехват разгона: разбор свапов, пороги входа и цикл наблюдения."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.sniper.momentum import (
    PoolStats,
    aggregate_swaps,
    evaluate_momentum,
    parse_swap,
    sample_age,
    token_is_token0,
)

TOKEN = "0x1111111111111111111111111111111111111111"
QUOTE = "0x2222222222222222222222222222222222222222"   # больше токена => токен это token0
ONE = 10**18


def v2_data(a0_in: int, a1_in: int, a0_out: int, a1_out: int) -> str:
    return "0x" + "".join(value.to_bytes(32, "big").hex() for value in (a0_in, a1_in, a0_out, a1_out))


def v3_data(amount0: int, amount1: int) -> str:
    """Swap V3: amount0/amount1 — знаковые int256, дальше цена, ликвидность и тик."""
    words = [(amount0 & (2**256 - 1)), (amount1 & (2**256 - 1)), 0, 0, 0]
    return "0x" + "".join(value.to_bytes(32, "big").hex() for value in words)


# ------------------------------------------------------------------- разбор
def test_token_order_matches_pool_rule():
    assert token_is_token0(TOKEN, QUOTE) is True
    assert token_is_token0(QUOTE, TOKEN) is False


def test_parse_v2_buy_and_sell():
    buy = parse_swap({"data": v2_data(0, ONE, 5_000, 0)}, token_is_first=True)
    assert buy == ("buy", ONE, 5_000)

    sell = parse_swap({"data": v2_data(5_000, 0, 0, ONE)}, token_is_first=True)
    assert sell == ("sell", ONE, 5_000)


def test_parse_v2_respects_token_position():
    """Тот же лог читается наоборот, если нативная монета — token0."""
    result = parse_swap({"data": v2_data(ONE, 0, 0, 5_000)}, token_is_first=False)
    assert result == ("buy", ONE, 5_000)


def test_parse_v3_signed_amounts():
    """В V3 знак говорит направление: плюс — актив зашёл в пул."""
    buy = parse_swap({"data": v3_data(-5_000, ONE)}, token_is_first=True, kind="v3")
    assert buy == ("buy", ONE, 5_000)

    sell = parse_swap({"data": v3_data(5_000, -ONE)}, token_is_first=True, kind="v3")
    assert sell == ("sell", ONE, 5_000)


def test_parse_ignores_broken_log():
    assert parse_swap({"data": "0x"}, token_is_first=True) is None
    assert parse_swap({"data": "0x" + "00" * 32}, token_is_first=True, kind="v3") is None


def test_aggregate_counts_sides_and_last_price():
    pools = {"0xaaa": {"token": TOKEN, "quote": QUOTE, "kind": "v2"}}
    logs = [
        {"address": "0xAAA", "data": v2_data(0, ONE, 5_000, 0)},          # покупка
        {"address": "0xAAA", "data": v2_data(0, 2 * ONE, 8_000, 0)},      # покупка
        {"address": "0xAAA", "data": v2_data(1_000, 0, 0, ONE // 10)},    # продажа
        {"address": "0xBBB", "data": v2_data(0, ONE, 1, 0)},              # чужой пул
    ]
    stats = aggregate_swaps(logs, pools)

    assert set(stats) == {"0xaaa"}
    bucket = stats["0xaaa"]
    assert (bucket.swaps, bucket.buys, bucket.sells) == (3, 2, 1)
    assert bucket.volume_native == 3 * ONE + ONE // 10
    assert bucket.buy_ratio == Decimal(2) / Decimal(3)
    assert bucket.last_price == Decimal(ONE // 10) / Decimal(1_000)


# ------------------------------------------------------------------ пороги
def stats_for(swaps: int = 10, buys: int = 8, price: str = "110", volume: int = ONE) -> PoolStats:
    return PoolStats(pool="0xaaa", swaps=swaps, buys=buys, sells=swaps - buys,
                     volume_native=volume, last_price=Decimal(price))


THRESHOLDS = {"min_gain_pct": 5, "max_gain_pct": 80, "min_trades": 5, "min_buy_ratio_pct": 60}


def test_signal_passes_on_early_move():
    signal = evaluate_momentum(Decimal(100), stats_for(), **THRESHOLDS)
    assert signal.passed, signal.reasons
    assert signal.gain_pct == Decimal(10)


def test_signal_rejects_top_of_the_pump():
    """Рост уже случился — заходить некуда, это самый дорогой вход."""
    signal = evaluate_momentum(Decimal(100), stats_for(price="300"), **THRESHOLDS)
    assert not signal.passed
    assert "вершине" in " ".join(signal.reasons)


def test_signal_rejects_flat_pool():
    signal = evaluate_momentum(Decimal(100), stats_for(price="101"), **THRESHOLDS)
    assert not signal.passed
    assert "движения пока нет" in " ".join(signal.reasons)


def test_signal_rejects_when_selling_prevails():
    signal = evaluate_momentum(Decimal(100), stats_for(buys=3), **THRESHOLDS)
    assert not signal.passed
    assert "продают больше" in " ".join(signal.reasons)


def test_signal_rejects_thin_activity():
    signal = evaluate_momentum(Decimal(100), stats_for(swaps=3, buys=3), **THRESHOLDS)
    assert not signal.passed
    assert "мало сделок" in " ".join(signal.reasons)


def test_signal_rejects_small_volume():
    signal = evaluate_momentum(Decimal(100), stats_for(volume=ONE // 100),
                               **THRESHOLDS, min_volume_wei=ONE)
    assert not signal.passed
    assert "оборот" in " ".join(signal.reasons)


def test_signal_without_previous_price_only_records():
    """Первый замер пула сравнивать не с чем — покупать по нему нельзя."""
    signal = evaluate_momentum(None, stats_for(), **THRESHOLDS)
    assert not signal.passed
    assert "нет предыдущего замера" in " ".join(signal.reasons)


def test_score_ranks_stronger_move_higher():
    strong = evaluate_momentum(Decimal(100), stats_for(price="150", swaps=30, buys=28), **THRESHOLDS)
    weak = evaluate_momentum(Decimal(100), stats_for(price="106", swaps=5, buys=3), **THRESHOLDS)
    assert strong.score > weak.score


def test_sample_age_treats_naive_time_as_utc():
    naive = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(seconds=90)
    assert 85 < sample_age(naive) < 95


# ------------------------------------------------------------------- хранение
async def test_pool_samples_roundtrip_and_pruning(db):
    async with session_scope() as session:
        await repo.add_pool_sample(session, chain="bsc", pool_address="0xaaa",
                                   token_address=TOKEN, price=Decimal("1.5"), swaps=4, buys=3,
                                   sells=1, volume_wei=ONE)
        await repo.add_pool_sample(session, chain="bsc", pool_address="0xaaa",
                                   token_address=TOKEN, price=Decimal("2.5"), swaps=6, buys=5,
                                   sells=1, volume_wei=2 * ONE)
        await repo.add_pool_sample(session, chain="bsc", pool_address="0xbbb",
                                   token_address=QUOTE, price=Decimal("9"), swaps=1)

    async with session_scope() as session:
        latest = await repo.last_pool_samples(session, "bsc", ["0xaaa", "0xbbb"])

    assert latest["0xaaa"].price == Decimal("2.5")     # берётся последний замер, не первый
    assert latest["0xbbb"].price == Decimal(9)

    async with session_scope() as session:
        removed = await repo.prune_pool_samples(session, dt.datetime.now(dt.UTC) + dt.timedelta(days=1))
        remaining = await repo.last_pool_samples(session, "bsc", ["0xaaa", "0xbbb"])

    assert removed == 3
    assert remaining == {}


async def test_watchlist_keeps_manual_and_recent_pools(db):
    old = dt.datetime.now(dt.UTC) - dt.timedelta(days=10)
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0xnew",
                                 token_address=TOKEN, status="rejected")
        manual = await repo.add_seen_pair(session, chain="bsc", pair_address="0xmanual",
                                          token_address=QUOTE, status="watch")
        stale = await repo.add_seen_pair(session, chain="bsc", pair_address="0xold",
                                         token_address=TOKEN, status="rejected")
        manual.created_at = old
        stale.created_at = old

    async with session_scope() as session:
        rows = await repo.momentum_watchlist(
            session, "bsc", dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)
        )

    pools = {row.pair_address for row in rows}
    assert pools == {"0xnew", "0xmanual"}   # отклонённый недавно — да, забытый старый — нет


# ------------------------------------------------------------------ цикл охоты
async def _subscriber(**overrides):
    """Пользователь с кошельком и включённым перехватом разгона."""
    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 1, "tester")
        user.wallet_address = "0x" + "a" * 40
        cfg = await repo.get_settings(session, 1, "bsc")
        cfg.auto_snipe = True
        cfg.momentum_enabled = True
        cfg.momentum_min_gain_pct = 5
        cfg.momentum_max_gain_pct = 80
        cfg.momentum_min_trades = 2
        cfg.momentum_min_buy_ratio_pct = 60
        cfg.momentum_min_volume = Decimal(0)
        cfg.min_liquidity = Decimal(0)
        for field, value in overrides.items():
            setattr(cfg, field, value)


def _hunter(monkeypatch, logs_by_call: list[list[dict]], bought: list):
    """Собирает охотника поверх заглушек сети и движка."""
    from sniperbot.chain.dex_adapter import PoolState
    from sniperbot.config import ChainConfig, RouterConfig, Settings
    from sniperbot.sniper import hunter as hunter_module

    router = RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)
    chain_config = ChainConfig(key="bsc", name="BNB", chain_id=56, enabled=True,
                               rpc_urls=["http://localhost"], wrapped_native=QUOTE,
                               routers=[router])
    calls = {"block": 0}

    class FakeClient:
        config = chain_config

        async def block_number(self):
            calls["block"] += 1
            return 100 * calls["block"]

        async def get_logs(self, params):  # noqa: ANN001
            return logs_by_call.pop(0) if logs_by_call else []

    class FakeRegistry:
        configs = {"bsc": chain_config}

        def get(self, key):  # noqa: ANN001
            return FakeClient()

        def config(self, key):  # noqa: ANN001
            return chain_config

    class StubAdapter:
        kind = "v2"
        name = "DEX"
        cfg = router

        async def pool_state(self, token, pool, decimals=18):  # noqa: ANN001
            return PoolState(pool=pool, liquidity_native=Decimal(9), reserve_native=9 * ONE)

    class StubEngine:
        registry = FakeRegistry()
        settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32, MOMENTUM_BACKFILL_BLOCKS=0)
        notifier = None

        async def ab_variant(self, user_id, chain, cfg):  # noqa: ANN001
            return "", cfg

        async def check_limits(self, user_id, chain, cfg):  # noqa: ANN001
            return None

        async def buy_for_user(self, user, cfg, event, report, venue, group="",
                               source="auto", headline=""):  # noqa: ANN001
            bought.append((event.token, source))

    monkeypatch.setattr(hunter_module, "get_adapter", lambda client, cfg: StubAdapter())
    monkeypatch.setattr(hunter_module, "analyze_token", _fake_report)
    monkeypatch.setattr(hunter_module, "evaluate_for_settings", lambda report, cfg: (True, []))
    return hunter_module.MomentumHunter(StubEngine())


async def _fake_report(*args, **kwargs):
    return object()


async def test_hunter_buys_only_after_price_confirms_the_move(db, monkeypatch):
    """Первый проход только запоминает цену: разгон виден лишь в сравнении окон."""
    await _subscriber()
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "c" * 40,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    pool_log = "0x" + "c" * 40
    flat = [{"address": pool_log, "data": v2_data(0, ONE, 1_000, 0)} for _ in range(3)]
    rising = [{"address": pool_log, "data": v2_data(0, 2 * ONE, 1_800, 0)} for _ in range(3)]

    bought: list = []
    hunter = _hunter(monkeypatch, [flat, rising], bought)

    await hunter.tick()          # первый проход только ставит курсор блока
    assert bought == []

    await hunter.tick()          # первый замер цены — сравнивать не с чем
    assert bought == []

    await hunter.tick()          # цена выросла на 11% при доле покупок 100%
    assert bought == [(TOKEN, "momentum")]


async def test_hunter_skips_pool_that_only_dumps(db, monkeypatch):
    await _subscriber()
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "c" * 40,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    pool_log = "0x" + "c" * 40
    first = [{"address": pool_log, "data": v2_data(0, ONE, 1_000, 0)} for _ in range(3)]
    dumping = [{"address": pool_log, "data": v2_data(1_000, 0, 0, 2 * ONE)} for _ in range(3)]

    bought: list = []
    hunter = _hunter(monkeypatch, [first, dumping], bought)
    for _ in range(3):
        await hunter.tick()

    assert bought == []
    assert hunter.trending["bsc"], "пул всё равно должен попадать в /trending"
    assert not hunter.trending["bsc"][0][0].passed


async def test_backfill_fills_watchlist_from_factory_history(db, monkeypatch):
    """Без разбора истории режим первые часы видит только свежие листинги."""
    from sniperbot.chain.abi import PAIR_CREATED_TOPIC
    from sniperbot.config import ChainConfig, RouterConfig, Settings
    from sniperbot.sniper import hunter as hunter_module

    router = RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)
    chain_config = ChainConfig(key="bsc", name="BNB", chain_id=56, enabled=True,
                               rpc_urls=["http://localhost"], wrapped_native=QUOTE,
                               routers=[router])
    pair = "0x" + "c" * 40
    created = {
        "topics": [PAIR_CREATED_TOPIC, "0x" + "0" * 24 + TOKEN[2:], "0x" + "0" * 24 + QUOTE[2:]],
        "data": "0x" + "00" * 12 + pair[2:] + "00" * 32,
        "blockNumber": 900,
    }
    ranges: list[tuple[int, int]] = []

    class FakeClient:
        config = chain_config

        async def block_number(self):
            return 2_500

        async def get_logs(self, params):  # noqa: ANN001
            ranges.append((params["fromBlock"], params["toBlock"]))
            return [created] if params["fromBlock"] <= 900 <= params["toBlock"] else []

    class StubEngine:
        registry = None
        settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32, MOMENTUM_BACKFILL_BLOCKS=2_000)
        notifier = None

    hunter = hunter_module.MomentumHunter(StubEngine())
    await hunter._backfill("bsc", FakeClient())

    assert ranges[0][0] == 500                     # 2500 − 2000 блоков истории
    async with session_scope() as session:
        rows = await repo.momentum_watchlist(
            session, "bsc", dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        )
    assert [row.pair_address.lower() for row in rows] == [pair]
    assert rows[0].status == "watch"

    await hunter._backfill("bsc", FakeClient())    # второй раз историю не перечитываем
    assert ranges[-1][1] == 2_500
    assert len([r for r in ranges if r[0] == 500]) == 1
