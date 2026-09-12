"""Жизнь позиции в боевом режиме: монитор, правила выхода и реальные продажи.

Отдельные правила проверены в test_exit_rules, но там нет ни базы, ни монитора.
Здесь проходит весь путь: позиция в базе, PositionMonitor.check_position, вызов
продажи и то, что осталось записано после каждого шага.
"""

from __future__ import annotations

from decimal import Decimal

from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position
from sniperbot.sniper.positions import PositionMonitor
from sniperbot.utils.fmt import from_wei, to_wei

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
ENTRY = Decimal("0.000001")


class FakeChain:
    key = "rh"
    name = "Robinhood Chain"
    native_decimals = 18
    native_symbol = "ETH"

    def address_url(self, address: str) -> str:
        return f"https://explorer/{address}"

    def token_url(self, address: str) -> str:
        return f"https://explorer/{address}"


class FakeClient:
    config = FakeChain()


class FakeRegistry:
    configs = {"rh": FakeChain()}

    def get(self, key):  # noqa: ANN001
        return FakeClient()

    def config(self, key):  # noqa: ANN001
        return FakeChain()


class FakeTrader:
    """Считает цену по курсу и исполняет продажи прямо в базе."""

    def __init__(self) -> None:
        self.price = ENTRY
        self.sales: list[tuple[int, int, str]] = []      # (позиция, процент, причина)
        self.quotes = 0
        self.dead = False

    async def sell_route(self, client, position, token, amount):  # noqa: ANN001
        self.quotes += 1
        if self.dead:
            return None
        return object(), object(), int(from_wei(amount) * self.price * 10**18)

    def adapter_for_position(self, position):  # noqa: ANN001
        return object()

    async def sell(self, user, position, *, cfg, percent, reason):  # noqa: ANN001
        self.sales.append((position.id, percent, reason))
        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            sold = stored.amount_wei * percent // 100
            stored.amount_wei -= sold
            stored.native_returned_wei += to_wei(from_wei(sold) * self.price)
            if stored.amount_wei <= 0:
                stored.status = "closed"
                stored.exit_reason = reason
        from sniperbot.sniper.executor import TradeResult

        return TradeResult(True, "sell", amount_out=to_wei(from_wei(sold) * self.price))


class Silent:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, user_id: int, text: str, **kwargs) -> None:
        self.messages.append(text)


def monitor(trader: FakeTrader, notifier: Silent) -> PositionMonitor:
    settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32)
    return PositionMonitor(FakeRegistry(), trader, notifier, settings)  # type: ignore[arg-type]


async def open_position(**kwargs) -> Position:
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        await repo.get_settings(session, 1, "rh")
        defaults = {
            "user_id": 1, "chain": "rh", "token_address": TOKEN, "token_symbol": "MEME",
            "token_decimals": 18, "router_address": "0x" + "r" * 40, "pair_address": "0x" + "p" * 40,
            "dex_kind": "v3", "pool_fee": 3000, "status": "open", "auto_sell": True,
            "amount_wei": to_wei(1000), "bought_wei": to_wei(1000),
            "native_spent_wei": to_wei("0.001"), "native_returned_wei": 0,
            "entry_price": ENTRY, "peak_price": ENTRY, "sell_percent": 100,
            "take_profit_pct": 0, "stop_loss_pct": 30, "trailing_stop_pct": 0,
            "tp_ladder": "", "tp_done": "", "secure_pct": 0, "breakeven_pct": 0,
            "rug_guard_pct": 0, "dead_timeout_min": 0, "dead_min_pct": 0,
        }
        defaults.update(kwargs)
        position = Position(**defaults)
        session.add(position)
        await session.flush()
        await session.refresh(position)
        return position


async def fresh(position_id: int) -> Position:
    async with session_scope() as session:
        return await session.get(Position, position_id)


# ------------------------------------------------------------- полный путь
async def test_full_life_of_a_winning_position(db):
    """Возврат вложенного на +40%, затем ступень ×10 — и позиция закрыта в плюс."""
    trader, notifier = FakeTrader(), Silent()
    position = await open_position(secure_pct=40, tp_ladder="900:100", trailing_stop_pct=0)
    watcher = monitor(trader, notifier)

    trader.price = ENTRY * Decimal("1.2")          # +20% — рано
    await watcher.check_position(await fresh(position.id))
    assert trader.sales == []

    trader.price = ENTRY * Decimal("1.45")         # +45% — возврат вложенного
    await watcher.check_position(await fresh(position.id))
    assert len(trader.sales) == 1
    _, percent, reason = trader.sales[0]
    assert reason == "secure" and 60 <= percent <= 80

    after = await fresh(position.id)
    assert after.status == "open"
    assert after.amount_wei > 0
    assert after.native_returned_wei >= after.native_spent_wei, "вложенное вернулось"
    assert "secure" in after.tp_done and after.breakeven_armed is True

    trader.price = ENTRY * 10                      # ×10 — ступень добирает хвост
    await watcher.check_position(await fresh(position.id))
    closed = await fresh(position.id)
    assert closed.status == "closed"
    assert closed.amount_wei == 0
    assert closed.native_returned_wei > closed.native_spent_wei * 3


async def test_stop_loss_closes_the_position(db):
    trader, notifier = FakeTrader(), Silent()
    position = await open_position(stop_loss_pct=30)
    trader.price = ENTRY * Decimal("0.6")          # −40%

    await monitor(trader, notifier).check_position(position)

    assert [reason for _, _, reason in trader.sales] == ["stop_loss"]
    assert (await fresh(position.id)).status == "closed"


async def test_breakeven_protects_the_tail_after_a_partial_take(db):
    """После частичной фиксации откат к входу закрывает остаток без убытка."""
    trader, notifier = FakeTrader(), Silent()
    position = await open_position(take_profit_pct=50, sell_percent=40)
    watcher = monitor(trader, notifier)

    trader.price = ENTRY * Decimal("1.6")
    await watcher.check_position(await fresh(position.id))
    assert trader.sales[-1][1] == 40

    trader.price = ENTRY                            # вернулись ко входу
    await watcher.check_position(await fresh(position.id))
    assert [reason for _, _, reason in trader.sales] == ["take_profit", "breakeven"]
    assert (await fresh(position.id)).status == "closed"


async def test_a_dead_pool_ends_as_a_write_off(db):
    """Котировки нет нигде — позиция списывается, а не висит вечно."""
    trader, notifier = FakeTrader(), Silent()
    position = await open_position()
    trader.dead = True
    watcher = monitor(trader, notifier)

    for _ in range(25):
        await watcher.check_position(await fresh(position.id))

    written = await fresh(position.id)
    assert written.status == "closed" and written.exit_reason == "stuck"
    assert trader.sales == []                       # продавать было нечего
    assert any("списана" in text for text in notifier.messages)

    async with session_scope() as session:
        assert await repo.count_open_positions(session, 1, "rh") == 0


async def test_a_quiet_pool_is_polled_less_often(db):
    """Иначе мёртвый пул опрашивается каждые полторы секунды до скончания века."""
    trader, notifier = FakeTrader(), Silent()
    await open_position()
    trader.dead = True
    watcher = monitor(trader, notifier)

    await watcher.tick()
    first = trader.quotes
    await watcher.tick()                            # сразу же — интервал ещё не вышел
    assert trader.quotes == first


# --------------------------------------------- экран не ждёт молчащую ноду
async def test_price_lookup_gives_up_instead_of_hanging(db):
    """Нода может думать десятками секунд — карточка столько ждать не должна."""
    import asyncio

    from sniperbot.bot.handlers.positions import _price

    class Slow(FakeTrader):
        async def sell_route(self, client, position, token, amount):  # noqa: ANN001
            await asyncio.sleep(30)

    class Ctx:
        registry = FakeRegistry()
        trader = Slow()
        notifier = Silent()
        settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32)

    position = await open_position()
    started = asyncio.get_running_loop().time()
    price = await _price(Ctx(), position, timeout=0.2)
    elapsed = asyncio.get_running_loop().time() - started

    assert price is None
    assert elapsed < 1, "ожидание должно обрываться по таймауту"
