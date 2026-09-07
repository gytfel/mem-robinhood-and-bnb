"""Тестовый режим (бумажные сделки) и риск-лимиты автоснайпа."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sniperbot.chain.dex_adapter import PoolRef
from sniperbot.chain.erc20 import TokenInfo
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, Position, User, utcnow
from sniperbot.sniper.engine import SniperEngine
from sniperbot.sniper.executor import Trader
from sniperbot.utils.fmt import to_wei

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
POOL = PoolRef(address="0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE", kind="v2")
RATE = 1000  # 1 нативная монета = 1000 токенов


class FakeAdapter:
    kind = "v2"
    name = "Fake DEX"
    router = "0x" + "r" * 40
    needs_unwrap = False

    def __init__(self, rate: int = RATE) -> None:
        self.rate = rate

    async def quote_sell(self, token, amount, pool):
        return amount // self.rate


class FakeChain:
    key = "bsc"
    native_decimals = 18
    native_symbol = "BNB"


class FakeRegistry:
    def config(self, key):  # noqa: ANN001
        return FakeChain()

    def get(self, key):  # noqa: ANN001
        return FakeChain()


def make_trader() -> Trader:
    return Trader(FakeRegistry(), None, None)  # type: ignore[arg-type]


def cfg_for(**kwargs) -> ChainSettings:
    defaults = {
        "user_id": 1, "chain": "bsc", "take_profit_pct": 100, "stop_loss_pct": 50,
        "trailing_stop_pct": 0, "auto_sell": True, "sell_percent": 100,
        "max_positions": 5, "max_snipes_per_hour": 10, "cooldown_seconds": 0,
        "daily_loss_limit": Decimal(0), "max_consecutive_losses": 0,
    }
    defaults.update(kwargs)
    return ChainSettings(**defaults)


# --------------------------------------------------------------- бумажные сделки
async def test_paper_buy_creates_position_without_transaction(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)

    trader = make_trader()
    token = TokenInfo(address=TOKEN, name="Meme", symbol="MEME", decimals=18)
    user = User(id=1, dry_run=True)

    result = await trader._paper_buy(
        user, "bsc", token, FakeAdapter(), POOL,
        spend_wei=to_wei("0.1"), expected=to_wei("100"), source="manual", cfg=cfg_for(),
    )

    assert result.ok is True
    assert result.tx_hash is None            # реальная транзакция не отправлялась
    async with session_scope() as session:
        position = await repo.find_position(session, result.position_id)
    assert position.is_paper is True
    assert position.native_spent_wei == to_wei("0.1")
    assert position.entry_price == Decimal("0.001")   # 0.1 BNB за 100 токенов


async def test_paper_sell_closes_position_by_quote(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(
            user_id=1, chain="bsc", token_address=TOKEN, token_symbol="MEME",
            router_address="0x" + "r" * 40, pair_address=POOL.address, is_paper=True,
            amount_wei=to_wei("100"), bought_wei=to_wei("100"), native_spent_wei=to_wei("0.1"),
            entry_price=Decimal("0.001"), status="open",
        ))

    async with session_scope() as session:
        position = (await repo.open_positions(session, user_id=1))[0]

    result = await make_trader()._paper_sell(position, FakeAdapter(), percent=100)
    assert result.ok is True

    async with session_scope() as session:
        stored = await repo.find_position(session, position.id)
    assert stored.status == "closed"
    assert stored.amount_wei == 0
    assert stored.native_returned_wei == to_wei("0.1")   # 100 токенов по курсу 1000


async def test_paper_positions_are_separate_in_reports(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        for is_paper, returned in ((True, "0.3"), (False, "0.05")):
            session.add(Position(
                user_id=1, chain="bsc", token_address=TOKEN, token_symbol="MEME",
                router_address="0x1", status="closed", is_paper=is_paper,
                native_spent_wei=to_wei("0.1"), native_returned_wei=to_wei(returned),
                closed_at=utcnow(),
            ))

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    async with session_scope() as session:
        paper = await repo.closed_between(session, 1, since, paper=True)
        real = await repo.closed_between(session, 1, since, paper=False)
    assert len(paper) == 1 and len(real) == 1
    assert paper[0].native_returned_wei == to_wei("0.3")
    assert real[0].native_returned_wei == to_wei("0.05")


# ------------------------------------------------------------------ риск-лимиты
def make_engine() -> SniperEngine:
    return SniperEngine(FakeRegistry(), None, None, None, None)  # type: ignore[arg-type]


async def test_position_and_hourly_limits(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                             status="open", source="auto", native_spent_wei=to_wei("0.1")))

    engine = make_engine()
    assert await engine._limits_hit(1, "bsc", cfg_for(max_positions=1)) is not None
    assert await engine._limits_hit(1, "bsc", cfg_for(max_positions=5, max_snipes_per_hour=1)) is not None
    assert await engine._limits_hit(1, "bsc", cfg_for(max_positions=5, max_snipes_per_hour=10)) is None


async def test_cooldown_blocks_and_expires(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                             status="closed", source="auto", native_spent_wei=to_wei("0.1"),
                             native_returned_wei=to_wei("0.2"), opened_at=utcnow(),
                             closed_at=utcnow()))

    engine = make_engine()
    blocked = await engine._limits_hit(1, "bsc", cfg_for(cooldown_seconds=600))
    assert blocked is not None and "пауза" in blocked
    assert await engine._limits_hit(1, "bsc", cfg_for(cooldown_seconds=0)) is None


async def test_daily_loss_limit_stops_sniping(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                             status="closed", source="auto", native_spent_wei=to_wei("0.5"),
                             native_returned_wei=to_wei("0.1"), closed_at=utcnow()))

    engine = make_engine()
    blocked = await engine._limits_hit(1, "bsc", cfg_for(daily_loss_limit=Decimal("0.3")))
    assert blocked is not None and "лимит убытка" in blocked
    # лимит выше убытка — снайп продолжается
    assert await engine._limits_hit(1, "bsc", cfg_for(daily_loss_limit=Decimal("1"))) is None


async def test_consecutive_losses_and_reset(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        for _ in range(3):
            session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                                 status="closed", source="auto", native_spent_wei=to_wei("0.1"),
                                 native_returned_wei=to_wei("0.02"), closed_at=utcnow()))

    engine = make_engine()
    blocked = await engine._limits_hit(1, "bsc", cfg_for(max_consecutive_losses=3))
    assert blocked is not None and "подряд" in blocked

    # /on ставит risk_reset_at «сейчас» — прошлые убытки больше не считаются
    reset = cfg_for(max_consecutive_losses=3)
    reset.risk_reset_at = utcnow() + dt.timedelta(seconds=1)
    assert await engine._limits_hit(1, "bsc", reset) is None


async def test_profitable_trade_breaks_the_losing_streak(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                             status="closed", native_spent_wei=to_wei("0.1"),
                             native_returned_wei=to_wei("0.02"),
                             closed_at=utcnow() - dt.timedelta(minutes=10)))
        session.add(Position(user_id=1, chain="bsc", token_address=TOKEN, router_address="0x1",
                             status="closed", native_spent_wei=to_wei("0.1"),
                             native_returned_wei=to_wei("0.5"), closed_at=utcnow()))

    async with session_scope() as session:
        assert await repo.consecutive_losses(session, 1, "bsc") == 0


# ------------------------------------------------- репутация создателей токенов
OWNER_BAD = "0xBAD0000000000000000000000000000000000001"
OWNER_GOOD = "0x9000000000000000000000000000000000000001"


async def test_bad_owners_collects_only_losing_creators(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        # на токене плохого владельца потеряли
        session.add(Position(user_id=1, chain="bsc", token_address="0xa", router_address="0x1",
                             status="closed", token_owner=OWNER_BAD,
                             native_spent_wei=to_wei("0.2"), native_returned_wei=to_wei("0.05"),
                             closed_at=utcnow()))
        # на токене хорошего — заработали
        session.add(Position(user_id=1, chain="bsc", token_address="0xb", router_address="0x1",
                             status="closed", token_owner=OWNER_GOOD,
                             native_spent_wei=to_wei("0.1"), native_returned_wei=to_wei("0.4"),
                             closed_at=utcnow()))

    async with session_scope() as session:
        bad = await repo.bad_owners(session, 1, "bsc")

    assert OWNER_BAD.lower() in bad
    assert OWNER_GOOD.lower() not in bad


async def test_owner_with_net_profit_is_forgiven(db):
    """Один убыток и одна крупная прибыль у того же владельца — не блокируем."""
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        for spent, returned in (("0.1", "0.02"), ("0.1", "0.9")):
            session.add(Position(user_id=1, chain="bsc", token_address="0xa", router_address="0x1",
                                 status="closed", token_owner=OWNER_BAD,
                                 native_spent_wei=to_wei(spent), native_returned_wei=to_wei(returned),
                                 closed_at=utcnow()))

    async with session_scope() as session:
        assert await repo.bad_owners(session, 1, "bsc") == set()


# ------------------------------------------- пулы, ждущие заливки ликвидности
async def test_waiting_pairs_are_returned_and_expired(db):
    old = utcnow() - dt.timedelta(hours=5)
    async with session_scope() as session:
        fresh = await repo.add_seen_pair(session, chain="bsc", pair_address="0xFRESH",
                                         token_address="0xT1", status="waiting")
        stale = await repo.add_seen_pair(session, chain="bsc", pair_address="0xSTALE",
                                         token_address="0xT2", status="waiting")
        stale.created_at = old

    since = utcnow() - dt.timedelta(hours=3)
    async with session_scope() as session:
        pending = await repo.waiting_pairs(session, "bsc", since)
    assert [row.pair_address for row in pending] == ["0xFRESH"]

    async with session_scope() as session:
        expired = await repo.expire_waiting_pairs(session, "bsc", since)
    assert expired == 1

    async with session_scope() as session:
        stale_row = await session.get(type(fresh), stale.id)
        assert stale_row.status == "rejected"
        assert "не появилась" in stale_row.reason
        assert (await repo.waiting_pairs(session, "bsc", since))[0].pair_address == "0xFRESH"


async def test_pair_status_counts(db):
    async with session_scope() as session:
        for address, status in (("0x1", "sniped"), ("0x2", "waiting"),
                                ("0x3", "waiting"), ("0x4", "rejected")):
            await repo.add_seen_pair(session, chain="bsc", pair_address=address,
                                     token_address=address, status=status)

    async with session_scope() as session:
        counts = await repo.pair_status_counts(session, "bsc")

    assert counts == {"sniped": 1, "waiting": 2, "rejected": 1}


async def test_watch_tick_processes_pair_once_liquidity_arrives(db, monkeypatch):
    """Пул без ликвидности не выбрасывается: бот вернётся к нему сам."""
    from decimal import Decimal

    from sniperbot.chain.dex_adapter import PoolState
    from sniperbot.config import ChainConfig, RouterConfig
    from sniperbot.db.models import SeenPair
    from sniperbot.sniper import engine as engine_module

    router = RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)
    chain_config = ChainConfig(key="bsc", name="BNB", chain_id=56, enabled=True,
                               rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40,
                               routers=[router])

    class FakeClient:
        config = chain_config

    class FakeRegistryWithConfig:
        configs = {"bsc": chain_config}

        def get(self, key):  # noqa: ANN001
            return FakeClient()

        def config(self, key):  # noqa: ANN001
            return chain_config

    liquid = {"value": False}

    class StubAdapter:
        kind = "v2"
        name = "DEX"

        async def pool_state(self, token, pool, decimals=18):  # noqa: ANN001
            return PoolState(pool=pool, liquidity_native=Decimal(5) if liquid["value"] else Decimal(0),
                             reserve_native=10**18 if liquid["value"] else 0)

    monkeypatch.setattr(engine_module, "get_adapter", lambda client, cfg: StubAdapter())

    processed: list = []
    engine = SniperEngine(FakeRegistryWithConfig(), None, None, None,  # type: ignore[arg-type]
                          engine_module.Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32))
    async def capture(event):  # noqa: ANN001
        processed.append(event)

    engine._process_pair = capture  # type: ignore[assignment]

    async with session_scope() as session:
        row = await repo.add_seen_pair(session, chain="bsc", pair_address="0xPOOL",
                                       token_address="0xTOKEN", router_address=router.router,
                                       status="waiting", block_number=100)
        row_id = row.id

    await engine.watch_tick()
    assert processed == []                       # ликвидности ещё нет — ждём дальше

    liquid["value"] = True
    await engine.watch_tick()
    assert len(processed) == 1
    assert processed[0].token == "0xTOKEN"

    async with session_scope() as session:
        assert (await session.get(SeenPair, row_id)).status == "checking"
