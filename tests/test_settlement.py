"""Отправленная транзакция не должна теряться: деньги списаны — позиция обязана появиться."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from sniperbot.chain.dex_adapter import PoolRef, PoolState
from sniperbot.config import ChainConfig, RouterConfig, Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.sniper import executor as executor_module
from sniperbot.sniper.executor import Trader

TOKEN = "0x" + "1" * 40
WALLET = "0x" + "a" * 40
ROUTER = RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)
CHAIN = ChainConfig(key="bsc", name="BNB", chain_id=56, enabled=True,
                    rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40,
                    routers=[ROUTER])


class FakeClient:
    config = CHAIN

    def __init__(self, receipt=None, error=None):
        self.receipt = receipt
        self.error = error

    async def wait_receipt(self, tx_hash, timeout=180):  # noqa: ANN001
        if self.error is not None:
            raise self.error
        return self.receipt


class FakeRegistry:
    configs = {"bsc": CHAIN}

    def __init__(self, client):
        self._client = client

    def get(self, key):  # noqa: ANN001
        return self._client

    def config(self, key):  # noqa: ANN001
        return CHAIN


class FakeWallets:
    def account(self, user):  # noqa: ANN001
        return SimpleNamespace(address=WALLET)


class StubAdapter:
    kind = "v2"
    name = "DEX"
    router = ROUTER.router
    cfg = ROUTER

    def __init__(self, price=Decimal("0.0002")):
        self.price = price

    async def pool_state(self, token, pool, decimals=18):  # noqa: ANN001
        return PoolState(pool=pool, price_native=self.price, reserve_native=10**18,
                         reserve_token=10**24, token_decimals=decimals)


def trader_for(client) -> Trader:
    return Trader(FakeRegistry(client), FakeWallets(),
                  Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32))


async def user_and_cfg():
    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 1, "u")
        user.wallet_address = WALLET
        cfg = await repo.get_settings(session, 1, "bsc")
        cfg.pre_approve = False
        return user, cfg


@pytest.fixture
def token():
    return SimpleNamespace(address=TOKEN, symbol="WIN", decimals=18, owner=None)


async def settle(trader, user, cfg, token, *, balance_after: int, timeout=1):
    async def fake_balance(client, address, holder):  # noqa: ANN001
        return balance_after

    original = executor_module.balance_of
    executor_module.balance_of = fake_balance
    try:
        return await trader.settle_buy(
            user, "bsc", token, StubAdapter(), PoolRef(address="0xpool", kind="v2"),
            spend_wei=10**16, balance_before=0, tx_hash="0xdead", source="auto",
            cfg=cfg, timeout=timeout,
        )
    finally:
        executor_module.balance_of = original


# ------------------------------------------------------- медленное подтверждение
async def test_slow_network_leaves_the_buy_pending_not_failed(db, token):
    """Истёкшее ожидание — не отказ: транзакция в сети, и её нельзя забыть."""
    trader = trader_for(FakeClient(error=TimeoutError("не дождался")))
    user, cfg = await user_and_cfg()

    result = await settle(trader, user, cfg, token, balance_after=0)

    assert result.ok is False
    assert result.pending is True          # именно «ждём», а не «не получилось»
    assert result.tx_hash == "0xdead"
    assert result.amount_in == 10**16      # сумма видна пользователю в уведомлении


async def test_broken_node_during_wait_is_also_pending(db, token):
    """Нода отвалилась после отправки — деньги потрачены, значит ждём, а не сдаёмся."""
    trader = trader_for(FakeClient(error=RuntimeError("нода недоступна")))
    user, cfg = await user_and_cfg()

    result = await settle(trader, user, cfg, token, balance_after=0)

    assert result.pending is True
    assert "не смог проверить" in (result.error or "").lower()


async def test_confirmed_transaction_opens_the_position(db, token):
    trader = trader_for(FakeClient(receipt={"status": 1, "gasUsed": 210_000}))
    user, cfg = await user_and_cfg()

    result = await settle(trader, user, cfg, token, balance_after=5 * 10**18)

    assert result.ok is True
    assert result.position_id is not None
    assert result.amount_out == 5 * 10**18

    async with session_scope() as session:
        positions = await repo.open_positions(session, user_id=1)
    assert len(positions) == 1
    assert positions[0].token_symbol == "WIN"


async def test_reverted_transaction_is_logged_as_failed(db, token):
    trader = trader_for(FakeClient(receipt={"status": 0}))
    user, cfg = await user_and_cfg()

    result = await settle(trader, user, cfg, token, balance_after=0)

    assert result.ok is False and result.pending is False
    assert "revert" in (result.error or "").lower()

    async with session_scope() as session:
        logs = await repo.recent_trades(session, user_id=1)
    assert logs and logs[0].status == "failed"


async def test_tokens_that_never_arrived_are_recorded_too(db, token):
    """Раньше такая сделка исчезала бесследно: газ потрачен, следов нет."""
    trader = trader_for(FakeClient(receipt={"status": 1}))
    user, cfg = await user_and_cfg()

    result = await settle(trader, user, cfg, token, balance_after=0)

    assert result.ok is False
    assert "не пришли" in (result.error or "")

    async with session_scope() as session:
        logs = await repo.recent_trades(session, user_id=1)
    assert logs and logs[0].status == "failed"


# --------------------------------------------------------------- подбор позиции
async def test_recover_adopts_tokens_sitting_in_the_wallet(db, token):
    """Монеты в кошельке без позиции — автопродажа их не видит и не защитит."""
    trader = trader_for(FakeClient())
    user, cfg = await user_and_cfg()

    async def fake_balance(client, address, holder):  # noqa: ANN001
        return [0, 1000 * 10**18]        # одна нода отстала, вторая видит токены

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original_balance, original_fetch = executor_module.balance_by_node, executor_module.fetch_token
    executor_module.balance_by_node = fake_balance
    executor_module.fetch_token = fake_token
    trader.best_venue = lambda *a, **kw: _venue()  # type: ignore[assignment]
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.balance_by_node = original_balance
        executor_module.fetch_token = original_fetch

    assert result.ok is True
    # 1000 токенов по 0.0002 = 0.2 монеты; единицы не должны разъезжаться
    assert result.amount_in == 2 * 10**17

    async with session_scope() as session:
        positions = await repo.open_positions(session, user_id=1)
    assert len(positions) == 1
    assert positions[0].source == "recover"
    assert positions[0].amount_wei == 1000 * 10**18


async def _venue():
    return StubAdapter(), PoolRef(address="0xpool", kind="v2")


async def test_recover_refuses_when_position_already_exists(db, token):
    trader = trader_for(FakeClient(receipt={"status": 1}))
    user, cfg = await user_and_cfg()
    await settle(trader, user, cfg, token, balance_after=5 * 10**18)

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original = executor_module.fetch_token
    executor_module.fetch_token = fake_token
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.fetch_token = original

    assert result.ok is False
    assert "уже открыта" in (result.error or "")
    # Проверка идёт до чтения баланса: незачем ходить в сеть за очевидным ответом.


async def test_recover_says_so_when_there_is_nothing_to_adopt(db, token):
    trader = trader_for(FakeClient())
    user, cfg = await user_and_cfg()

    async def fake_balance(client, address, holder):  # noqa: ANN001
        return [0, 0]                    # все ноды согласны: токенов нет

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original_balance, original_fetch = executor_module.balance_by_node, executor_module.fetch_token
    executor_module.balance_by_node = fake_balance
    executor_module.fetch_token = fake_token
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.balance_by_node = original_balance
        executor_module.fetch_token = original_fetch

    assert result.ok is False
    assert "нет этого токена" in (result.error or "")


# --------------------------------------------- ложный ноль баланса при продаже
class BalanceClient(FakeClient):
    """Ноды отвечают по-разному: одна отстала и занижает баланс."""

    def __init__(self, answers: list[int], single: int = 0):
        super().__init__(receipt={"status": 1})
        self.answers = answers
        self.single = single
        self.calls = 0

    async def call_all(self, address, abi, fn_name, *args):  # noqa: ANN001
        self.calls += 1
        return list(self.answers)

    async def gas_fees(self, multiplier=1.0, priority_gwei=1.0):  # noqa: ANN001
        # Дальше проверки баланса тесту идти незачем: важно лишь, что позиция
        # дожила до настоящей продажи, а не была списана.
        raise RuntimeError("остановка теста после проверки баланса")


async def test_confirmed_balance_takes_the_highest_answer():
    """Отставшая нода занижает баланс, но завысить его не может."""
    from sniperbot.chain.erc20 import confirmed_balance

    client = BalanceClient([0, 30_000 * 10**18, 0])
    assert await confirmed_balance(client, TOKEN, WALLET) == 30_000 * 10**18


async def test_confirmed_balance_is_zero_when_every_node_agrees():
    from sniperbot.chain.erc20 import confirmed_balance

    assert await confirmed_balance(BalanceClient([0, 0]), TOKEN, WALLET) == 0
    assert await confirmed_balance(BalanceClient([]), TOKEN, WALLET) == 0


async def sell_with(monkeypatch, db_position, first_read: int, all_nodes: list[int]):
    """Продажа, где первый (обычный) запрос баланса вернул first_read."""
    client = BalanceClient(all_nodes)
    trader = trader_for(client)
    user, cfg = await user_and_cfg()

    async def fake_balance(client_, address, holder):  # noqa: ANN001
        return first_read

    monkeypatch.setattr(executor_module, "balance_of", fake_balance)
    monkeypatch.setattr(executor_module, "BALANCE_RECHECK_DELAY", 0)
    trader.adapter_for_position = lambda position: StubAdapter()  # type: ignore[assignment]
    return trader, await trader.sell(user, db_position, cfg=cfg, percent=100), client


async def open_position(db_token) -> object:
    trader = trader_for(FakeClient(receipt={"status": 1}))
    user, cfg = await user_and_cfg()
    await settle(trader, user, cfg, db_token, balance_after=30_000 * 10**18)
    async with session_scope() as session:
        return (await repo.open_positions(session, user_id=1))[0]


async def test_lagging_node_must_not_write_off_the_position(db, token, monkeypatch):
    """Главный случай: ноль от одной ноды не повод вычёркивать токены."""
    position = await open_position(token)

    with pytest.raises(RuntimeError, match="остановка теста"):
        await sell_with(monkeypatch, position, first_read=0, all_nodes=[0, 30_000 * 10**18])

    async with session_scope() as session:
        stored = await session.get(executor_module.Position, position.id)
    assert stored.status == "open"              # позиция уцелела
    assert stored.exit_reason != "lost"


async def test_position_is_written_off_only_when_all_nodes_agree(db, token, monkeypatch):
    position = await open_position(token)

    _, result, client = await sell_with(monkeypatch, position, first_read=0, all_nodes=[0, 0])

    assert result.ok is False
    assert "нет на кошельке" in (result.error or "")
    assert "/recover" in (result.error or "")   # подсказка, как вернуть

    async with session_scope() as session:
        stored = await session.get(executor_module.Position, position.id)
    assert stored.status == "closed"
    assert stored.exit_reason == "lost"         # отличимо от обычной продажи


async def test_recover_reopens_a_lost_position_instead_of_duplicating(db, token):
    """Иначе убыток по старой записи посчитался бы вторым разом."""
    position = await open_position(token)
    spent = position.native_spent_wei
    async with session_scope() as session:
        stored = await session.get(executor_module.Position, position.id)
        stored.status = "closed"
        stored.exit_reason = "lost"
        stored.amount_wei = 0

    trader = trader_for(FakeClient())
    user, cfg = await user_and_cfg()

    async def fake_balance(client, address, holder):  # noqa: ANN001
        return [30_000 * 10**18]

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original_balance, original_fetch = executor_module.balance_by_node, executor_module.fetch_token
    executor_module.balance_by_node = fake_balance
    executor_module.fetch_token = fake_token
    trader.best_venue = lambda *a, **kw: _venue()  # type: ignore[assignment]
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.balance_by_node = original_balance
        executor_module.fetch_token = original_fetch

    assert result.ok is True
    assert result.position_id == position.id     # та же запись, а не вторая
    assert result.amount_in == spent             # исходная сумма покупки сохранена

    async with session_scope() as session:
        positions = await repo.open_positions(session, user_id=1)
    assert len(positions) == 1
    assert positions[0].amount_wei == 30_000 * 10**18


async def test_recover_survives_a_lagging_node(db, token):
    """Подбор — инструмент как раз для ложного нуля: одного ответа тут мало."""
    trader = trader_for(FakeClient())
    user, cfg = await user_and_cfg()

    async def one_node_lags(client, address, holder):  # noqa: ANN001
        return [0, 0, 500 * 10**18]

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original, original_fetch = executor_module.balance_by_node, executor_module.fetch_token
    executor_module.balance_by_node = one_node_lags
    executor_module.fetch_token = fake_token
    trader.best_venue = lambda *a, **kw: _venue()  # type: ignore[assignment]
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.balance_by_node = original
        executor_module.fetch_token = original_fetch

    assert result.ok is True
    assert result.amount_out == 500 * 10**18


async def test_recover_failure_says_how_many_nodes_were_asked(db, token):
    """Пользователь должен видеть, на чём основан отказ, а не верить на слово."""
    trader = trader_for(FakeClient())
    user, cfg = await user_and_cfg()

    async def all_zero(client, address, holder):  # noqa: ANN001
        return [0, 0, 0]

    async def fake_token(client, address):  # noqa: ANN001
        return token

    original, original_fetch = executor_module.balance_by_node, executor_module.fetch_token
    executor_module.balance_by_node = all_zero
    executor_module.fetch_token = fake_token
    try:
        result = await trader.adopt(user, "bsc", TOKEN, cfg=cfg)
    finally:
        executor_module.balance_by_node = original
        executor_module.fetch_token = original_fetch

    assert result.ok is False
    assert "Опросил нод: 3" in (result.error or "")
    assert "Проверить самому" in (result.error or "")
