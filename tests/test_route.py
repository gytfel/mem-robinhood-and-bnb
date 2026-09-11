"""Маршрут (`/route`): где боту разрешено входить в сделку.

Выбранная площадка — это ограничение на вход, а не пожелание. Проверяется в
трёх местах, где деньги могут уйти: снайп новой пары, перехват разгона и сам
исполнитель сделки.
"""

from __future__ import annotations

from decimal import Decimal

from sniperbot.chain.dex_adapter import PoolRef, available_kinds, route_allows
from sniperbot.config import ChainConfig, RouterConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.sniper.executor import Trader
from sniperbot.sniper.scanner import PairEvent

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
QUOTE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
V2_ROUTER = RouterConfig("DEX V2", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)
V3_ROUTER = RouterConfig("DEX V3", "0x" + "3" * 40, "0x" + "e" * 40, 30, True,
                         kind="v3", quoter="0x" + "q" * 40)


# ------------------------------------------------------------------- правило
def test_auto_allows_every_venue():
    for kind in ("v2", "v3"):
        assert route_allows("auto", kind) is True
        assert route_allows("", kind) is True
        assert route_allows(None, kind) is True


def test_fixed_route_allows_only_its_own_version():
    assert route_allows("v3", "v3") is True
    assert route_allows("v3", "v2") is False
    assert route_allows("v2", "v2") is True
    assert route_allows("v2", "v3") is False


def test_case_and_missing_kind_do_not_open_a_hole():
    """Пустая версия пула — это V2: пул V2 не должен пролезть под маршрутом v3."""
    assert route_allows(" V3 ", "V3") is True
    assert route_allows("v3", None) is False
    assert route_allows("v2", None) is True


def test_available_kinds_counts_only_configured_venues():
    half = RouterConfig("DEX V3", "0x" + "3" * 40, "0x" + "e" * 40, 30, True, kind="v3")
    chain = ChainConfig(key="bsc", name="BNB", chain_id=56, rpc_urls=["http://localhost"],
                        wrapped_native=QUOTE, routers=[V2_ROUTER, half])
    # У V3 без Quoter котировку взять нечем — такой площадки у сети фактически нет.
    assert available_kinds(chain) == {"v2"}

    full = ChainConfig(key="bsc", name="BNB", chain_id=56, rpc_urls=["http://localhost"],
                       wrapped_native=QUOTE, routers=[V2_ROUTER, V3_ROUTER])
    assert available_kinds(full) == {"v2", "v3"}


# ------------------------------------------------------------- поиск площадки
class FakeAdapter:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.name = f"DEX {kind}"

    async def find_pool(self, token):  # noqa: ANN001
        return PoolRef(address="0x" + self.kind[-1] * 40, kind=self.kind)

    async def pool_state(self, token, pool, decimals=18):  # noqa: ANN001
        from sniperbot.chain.dex_adapter import PoolState

        depth = Decimal(10) if self.kind == "v2" else Decimal(1)
        return PoolState(pool=pool, liquidity_native=depth, reserve_native=int(depth) * 10**18)


async def test_route_beats_liquidity_when_choosing_a_venue(monkeypatch):
    """Смысл жёсткого маршрута в том, чтобы не уходить на V2, даже если там глубже."""
    from sniperbot.chain import dex_adapter

    monkeypatch.setattr(dex_adapter, "adapters_for",
                        lambda client: [FakeAdapter("v2"), FakeAdapter("v3")])

    best = await dex_adapter.find_best_venue(None, TOKEN)
    assert best[0].kind == "v2"          # auto: где ликвидности больше

    fixed = await dex_adapter.find_best_venue(None, TOKEN, route="v3")
    assert fixed[0].kind == "v3"         # маршрут: только своя версия


# ------------------------------------------------------------------ исполнитель
class FakeChain:
    key = "bsc"
    native_decimals = 18
    native_symbol = "BNB"


class FakeRegistry:
    def config(self, key):  # noqa: ANN001
        return FakeChain()

    def get(self, key):  # noqa: ANN001
        return FakeChain()


def _trader(monkeypatch, pools: dict[str, str]) -> Trader:
    """Исполнитель поверх сети, где у токена есть перечисленные площадки."""
    from sniperbot.sniper import executor as executor_module

    async def fake_find(client, token, decimals=18, route="auto"):  # noqa: ANN001
        for kind, address in pools.items():
            if route_allows(route, kind):
                return (FakeAdapter(kind), PoolRef(address=address, kind=kind), None)
        return None

    monkeypatch.setattr(executor_module, "find_best_venue", fake_find)
    return Trader(FakeRegistry(), None, None)  # type: ignore[arg-type]


async def test_scanner_hint_of_the_wrong_version_is_ignored(monkeypatch):
    """Сканер нашёл пару V2, а вход разрешён только на V3 — подсказку не берём."""
    trader = _trader(monkeypatch, {"v3": "0x" + "3" * 40})
    hint = (FakeAdapter("v2"), PoolRef(address="0x" + "2" * 40, kind="v2"))

    chosen = await trader.resolve_venue("bsc", TOKEN, "v3", hint)
    assert chosen is not None and chosen[1].kind == "v3"

    kept = await trader.resolve_venue("bsc", TOKEN, "auto", hint)
    assert kept is hint                 # без ограничения подсказка экономит запросы


async def test_hint_without_a_matching_pool_ends_in_refusal(monkeypatch):
    trader = _trader(monkeypatch, {"v2": "0x" + "2" * 40})
    hint = (FakeAdapter("v2"), PoolRef(address="0x" + "2" * 40, kind="v2"))
    assert await trader.resolve_venue("bsc", TOKEN, "v3", hint) is None


async def test_refusal_explains_that_the_route_cut_the_pool_off(monkeypatch):
    trader = _trader(monkeypatch, {"v2": "0x" + "2" * 40})
    text = await trader._no_venue_error("bsc", TOKEN, "v3")
    assert "V3" in text and "V2" in text and "/route auto" in text

    nothing = _trader(monkeypatch, {})
    assert "ни на одном DEX" in await nothing._no_venue_error("bsc", TOKEN, "auto")


# ----------------------------------------------------------------- снайп пары
def _engine(monkeypatch, waited: list):
    from sniperbot.config import Settings
    from sniperbot.sniper import engine as engine_module

    chain = ChainConfig(key="bsc", name="BNB", chain_id=56, enabled=True,
                        rpc_urls=["http://localhost"], wrapped_native=QUOTE,
                        routers=[V2_ROUTER, V3_ROUTER])

    class FakeClient:
        config = chain

    class Registry:
        configs = {"bsc": chain}

        def get(self, key):  # noqa: ANN001
            return FakeClient()

        def config(self, key):  # noqa: ANN001
            return chain

    engine = engine_module.SniperEngine(
        Registry(), None, None, None,  # type: ignore[arg-type]
        Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32),
    )
    monkeypatch.setattr(engine_module, "get_adapter", lambda client, cfg: FakeAdapter(cfg.kind))

    async def fake_wait(adapter, event, pool):  # noqa: ANN001
        waited.append(event.pair)
        return False        # дальше ликвидности дело не идёт — этого хватает

    monkeypatch.setattr(engine, "_wait_for_liquidity", fake_wait)
    return engine


async def _pair(chain: str = "bsc", kind: str = "v2") -> PairEvent:
    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 1, "tester")
        user.wallet_address = "0x" + "a" * 40
        cfg = await repo.get_settings(session, 1, chain)
        cfg.auto_snipe = True
        row = await repo.add_seen_pair(session, chain=chain, pair_address="0x" + "c" * 40,
                                       token_address=TOKEN, dex_kind=kind, status="new")
        pair_id = row.id
    return PairEvent(chain=chain, pair="0x" + "c" * 40, token=TOKEN, quote=QUOTE, block=10,
                     router=V2_ROUTER if kind == "v2" else V3_ROUTER, kind=kind, fee=0,
                     pair_id=pair_id)


async def _set_route(value: str) -> None:
    async with session_scope() as session:
        cfg = await repo.get_settings(session, 1, "bsc")
        cfg.dex_route = value


async def _pair_row(pair_id: int):
    async with session_scope() as session:
        rows = await repo.recent_pairs(session, "bsc")
    return next(row for row in rows if row.id == pair_id)


async def test_sniper_does_not_touch_a_pair_outside_the_route(db, monkeypatch):
    """Главная жалоба: маршрут v3, а бот покупал новые пары V2."""
    event = await _pair(kind="v2")
    await _set_route("v3")

    waited: list = []
    await _engine(monkeypatch, waited)._process_pair_inner(event)

    assert waited == [], "пара не нашего маршрута не должна доходить даже до ожидания ликвидности"
    row = await _pair_row(event.pair_id)
    assert row.status == "rejected"
    assert row.reject_codes == "route"     # /stats должен уметь показать эту причину


async def test_matching_pair_goes_on_as_before(db, monkeypatch):
    event = await _pair(kind="v3")
    await _set_route("v3")

    waited: list = []
    await _engine(monkeypatch, waited)._process_pair_inner(event)
    assert waited == [event.pair]


async def test_auto_keeps_taking_both_versions(db, monkeypatch):
    event = await _pair(kind="v2")
    await _set_route("auto")

    waited: list = []
    await _engine(monkeypatch, waited)._process_pair_inner(event)
    assert waited == [event.pair]


# --------------------------------------------------------------- перехват разгона
async def test_momentum_ignores_pools_outside_the_route(db, monkeypatch):
    """Разгон на V2 при маршруте v3 — тоже не наша сделка."""
    from tests.test_momentum import _hunter, _subscriber, v2_data

    await _subscriber(dex_route="v3")
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "c" * 40,
                                 token_address="0x" + "1" * 40, router_address="0x" + "r" * 40,
                                 dex_kind="v2", status="rejected")

    pool = "0x" + "c" * 40
    flat = [{"address": pool, "data": v2_data(0, 10**18, 1_000, 0)} for _ in range(3)]
    rising = [{"address": pool, "data": v2_data(0, 2 * 10**18, 1_800, 0)} for _ in range(3)]

    bought: list = []
    hunter = _hunter(monkeypatch, [flat, rising], bought)
    for _ in range(3):
        await hunter.tick()

    assert bought == []
    # Пул V2 при маршруте v3 не доходит даже до наблюдения: место в списке
    # (и адрес в запросе логов) достаётся тем пулам, которые можно купить.
    assert hunter.watched["bsc"] == 0
    assert hunter.trending.get("bsc", []) == []


async def test_momentum_still_buys_on_the_chosen_venue(db, monkeypatch):
    from tests.test_momentum import _hunter, _subscriber, v2_data

    await _subscriber(dex_route="v2")
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "c" * 40,
                                 token_address="0x" + "1" * 40, router_address="0x" + "r" * 40,
                                 dex_kind="v2", status="rejected")

    pool = "0x" + "c" * 40
    flat = [{"address": pool, "data": v2_data(0, 10**18, 1_000, 0)} for _ in range(3)]
    rising = [{"address": pool, "data": v2_data(0, 2 * 10**18, 1_800, 0)} for _ in range(3)]

    bought: list = []
    hunter = _hunter(monkeypatch, [flat, rising], bought)
    for _ in range(3):
        await hunter.tick()

    assert [source for _, source in bought] == ["momentum"]


async def test_watchlist_filter_treats_old_rows_as_v2(db):
    """У записей до появления V3 версия пуста — иначе они пропали бы из наблюдения."""
    import datetime as dt

    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "a" * 40,
                                 token_address=TOKEN, status="new")                    # без версии
        await repo.add_seen_pair(session, chain="bsc", pair_address="0x" + "b" * 40,
                                 token_address=TOKEN, dex_kind="v3", status="new")

    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    async with session_scope() as session:
        v2_only = await repo.momentum_watchlist(session, "bsc", since, kinds={"v2"})
        v3_only = await repo.momentum_watchlist(session, "bsc", since, kinds={"v3"})
        everything = await repo.momentum_watchlist(session, "bsc", since)

    assert [row.pair_address for row in v2_only] == ["0x" + "a" * 40]
    assert [row.pair_address for row in v3_only] == ["0x" + "b" * 40]
    assert len(everything) == 2


# ------------------------------------------------------------------ подсказка
def test_warning_fires_only_when_the_venue_is_missing():
    from sniperbot.bot.texts import route_warning

    only_v2 = ChainConfig(key="bsc", name="BNB", chain_id=56, rpc_urls=["http://localhost"],
                          wrapped_native=QUOTE, routers=[V2_ROUTER])
    warning = route_warning(only_v2, "v3")
    assert "BSC_V3_ROUTER" in warning and "не купит ничего" in warning

    assert route_warning(only_v2, "v2") == ""
    assert route_warning(only_v2, "auto") == ""

    both = ChainConfig(key="bsc", name="BNB", chain_id=56, rpc_urls=["http://localhost"],
                       wrapped_native=QUOTE, routers=[V2_ROUTER, V3_ROUTER])
    assert route_warning(both, "v3") == ""
