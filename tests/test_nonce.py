"""Нумерация транзакций: занятый и неотправленный nonce ломает следующий выход.

Реальный случай: при сливе ликвидности бот трижды пытался продать позицию.
Первая попытка сорвалась на симуляции, но номер уже был занят — и сеть
ответила на следующую «nonce too high: tx: 33 state: 32». Позиция осталась
в токене, который в это время падал.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eth_account import Account

from sniperbot.chain.wallet import WalletError, WalletService

CHAIN = SimpleNamespace(key="rh", chain_id=8_000, native_symbol="ETH")


class FakeClient:
    """Узел, который знает свой nonce и может отказать в приёме."""

    config = CHAIN

    def __init__(self, state_nonce: int = 32, errors: list[str] | None = None) -> None:
        self.state_nonce = state_nonce
        self.errors = list(errors or [])
        self.accepted: list[int] = []
        self.asked = 0

    async def transaction_count(self, address, block="pending"):  # noqa: ANN001
        self.asked += 1
        return self.state_nonce

    async def send_raw(self, raw):  # noqa: ANN001
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        self.accepted.append(len(self.accepted))
        return "0x" + "d" * 64


@pytest.fixture
def wallets(vault):
    return WalletService(vault)


@pytest.fixture
def account():
    return Account.from_key("0x" + "7" * 64)


def tx_for(account) -> dict:  # noqa: ANN001
    return {"to": account.address, "value": 0, "gas": 21_000, "gasPrice": 10**9}


# --------------------------------------------------------------- выдача номера
async def test_nonce_comes_from_the_network_at_send_time(wallets, account):
    """Без заранее занятого номера первая транзакция уходит с номером сети."""
    client = FakeClient(state_nonce=32)

    sent = await wallets.send_tx(client, account, tx_for(account))

    assert sent.nonce == 32


async def test_probe_nonce_reserves_nothing(wallets, account):
    """Номер для симуляции не должен смещать счётчик: иначе появится дыра."""
    client = FakeClient(state_nonce=32)

    probe = await wallets.probe_nonce(client, account.address)
    sent = await wallets.send_tx(client, account, tx_for(account))

    assert probe == 32
    assert sent.nonce == 32, "симуляция съела номер — сеть будет ждать пропущенный"


async def test_probe_nonce_survives_a_silent_node(wallets, account):
    """Нода не ответила — сборке хватит нуля, настоящий номер выдаст отправка."""

    class Broken(FakeClient):
        async def transaction_count(self, address, block="pending"):  # noqa: ANN001
            raise RuntimeError("нода отвалилась")

    assert await wallets.probe_nonce(Broken(), account.address) == 0


async def test_two_sends_in_a_row_get_consecutive_numbers(wallets, account):
    client = FakeClient(state_nonce=32)

    first = await wallets.send_tx(client, account, tx_for(account))
    second = await wallets.send_tx(client, account, tx_for(account))

    assert (first.nonce, second.nonce) == (32, 33)


# ------------------------------------------------------- рассинхрон со счётчиком
async def test_nonce_too_high_is_resynced_and_retried(wallets, account):
    """Главный случай: счётчик ушёл вперёд — повторяем номером из сети."""
    client = FakeClient(
        state_nonce=32,
        errors=["{'code': -32000, 'message': 'nonce too high: tx: 33 state: 32'}"],
    )
    # Счётчик уже сместился прошлой сорвавшейся попыткой.
    wallets.nonces._next[("rh", account.address.lower())] = 33

    sent = await wallets.send_tx(client, account, tx_for(account))

    assert sent.nonce == 32, "повтор обязан взять номер у сети, а не свой"
    assert client.accepted, "транзакция так и не ушла"


async def test_nonce_too_high_twice_is_explained_in_russian(wallets, account):
    """Если и повтор не прошёл — пользователь читает объяснение, а не ответ ноды."""
    message = "{'code': -32000, 'message': 'nonce too high: tx: 33 state: 32'}"
    client = FakeClient(state_nonce=32, errors=[message, message])

    with pytest.raises(WalletError) as exc:
        await wallets.send_tx(client, account, tx_for(account))

    assert "счётчик ушёл вперёд" in str(exc.value)
    assert "nonce too high" not in str(exc.value)


async def test_nonce_too_low_is_not_retried(wallets, account):
    """Такой номер может быть уже в блоке: повтор означал бы вторую сделку."""
    client = FakeClient(state_nonce=32, errors=["nonce too low"])

    with pytest.raises(WalletError) as exc:
        await wallets.send_tx(client, account, tx_for(account))

    assert "устарел" in str(exc.value)
    assert client.accepted == [], "повторная отправка при too low недопустима"


async def test_other_errors_keep_their_text(wallets, account):
    client = FakeClient(state_nonce=32, errors=["insufficient funds for gas"])

    with pytest.raises(WalletError) as exc:
        await wallets.send_tx(client, account, tx_for(account))

    assert "Недостаточно средств" in str(exc.value)


async def test_a_failed_send_does_not_shift_the_counter(wallets, account):
    """После отказа сети следующая попытка берёт номер заново, без смещения."""
    client = FakeClient(state_nonce=32, errors=["insufficient funds for gas"])

    with pytest.raises(WalletError):
        await wallets.send_tx(client, account, tx_for(account))
    sent = await wallets.send_tx(client, account, tx_for(account))

    assert sent.nonce == 32


# ------------------------------------------------- сквозная проверка через Trader
class SellNode:
    """Узел, который сначала отклоняет симуляцию, а со второго раза принимает всё."""

    def __init__(self, chain, state_nonce: int = 32) -> None:
        self.config = chain
        self.state_nonce = state_nonce
        self.simulate_ok = False
        self.accepted: list[int] = []
        self.raw_txs: list[bytes] = []

    async def transaction_count(self, address, block="pending"):  # noqa: ANN001
        return self.state_nonce

    async def raw_call(self, call, overrides=None):  # noqa: ANN001
        if not self.simulate_ok:
            raise RuntimeError("('execution reverted', '0x')")
        return b"\x01"

    async def estimate_gas(self, tx):  # noqa: ANN001
        return 200_000

    async def gas_fees(self, multiplier=1.0, priority=1.0):  # noqa: ANN001
        return {"gasPrice": 10**9}

    async def send_raw(self, raw):  # noqa: ANN001
        self.raw_txs.append(raw)
        return "0x" + "e" * 64

    async def wait_receipt(self, tx_hash, timeout=180):  # noqa: ANN001
        return {"status": 1, "gasUsed": 180_000, "effectiveGasPrice": 10**9}

    async def native_balance(self, address):  # noqa: ANN001
        return 10**18


async def test_a_failed_simulation_does_not_burn_a_nonce(db, vault, monkeypatch):
    """Сквозь настоящий Trader.sell: сорвавшаяся попытка не должна смещать счётчик.

    Ровно этот случай стоил выхода из позиции на реальных деньгах: первая
    продажа не прошла симуляцию, а вторая получила от узла «nonce too high».
    """
    from decimal import Decimal

    from sniperbot.config import ChainConfig, RouterConfig, Settings
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope
    from sniperbot.db.models import Position
    from sniperbot.sniper import executor as executor_module
    from sniperbot.sniper.executor import Trader
    from sniperbot.utils.fmt import to_wei

    token = "0x55d398326f99059fF775485246999027B3197955"
    ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
    FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
    WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
    PAIR = "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"
    chain = ChainConfig(
        key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH", enabled=True,
        rpc_urls=["http://localhost"], wrapped_native=WNATIVE,
        routers=[RouterConfig("DEX", ROUTER, FACTORY, 25, True)],
    )
    node = SellNode(chain)

    class Registry:
        configs = {"rh": chain}

        def get(self, key):  # noqa: ANN001
            return node

        def config(self, key):  # noqa: ANN001
            return chain

    class Adapter:
        kind, name = "v2", "DEX"
        router = spender = ROUTER
        needs_unwrap = False

        async def quote_sell(self, token, amount, pool):  # noqa: ANN001
            return to_wei("0.002")

        async def build_sell_tx(self, token, wallet, amount, minimum, pool, *,
                                nonce, gas_limit, gas_fees):  # noqa: ANN001
            return {"to": self.router, "data": "0x01", "nonce": nonce, "gas": gas_limit,
                    **gas_fees}

        def try_next_variant(self):
            return False          # у V2 одна кодировка свапа

    wallets = WalletService(vault)
    trader = Trader(Registry(), wallets, Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32))
    monkeypatch.setattr(trader, "adapter_for_position", lambda position: Adapter())
    monkeypatch.setattr(executor_module, "balance_of", lambda *a, **kw: _amount())
    monkeypatch.setattr(executor_module, "allowance", lambda *a, **kw: _huge())

    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 1, "u")
        wallets.ensure_wallet(user)
        cfg = await repo.get_settings(session, 1, "rh")
        position = Position(
            user_id=1, chain="rh", token_address=token, token_symbol="MEME",
            token_decimals=18, router_address=Adapter.router, pair_address=PAIR,
            dex_kind="v2", pool_fee=0, status="open", amount_wei=to_wei(1000),
            bought_wei=to_wei(1000), native_spent_wei=to_wei("0.001"),
            native_returned_wei=0, entry_price=Decimal("0.000001"),
        )
        session.add(position)
        await session.flush()
        await session.refresh(position)
        session.expunge_all()

    # Первая попытка: контракт отклоняет продажу в симуляции.
    first = await trader.sell(user, position, cfg=cfg, percent=100, reason="rug")
    assert first.ok is False
    assert "контракт отклонил сделку" in (first.error or "")

    # Вторая попытка: контракт согласен. Номер обязан быть тот же, что ждёт сеть.
    node.simulate_ok = True
    second = await trader.sell(user, position, cfg=cfg, percent=100, reason="rug")

    assert second.ok is True, second.error
    assert wallets.nonces._next[("rh", user.wallet_address.lower())] == 33
    assert len(node.raw_txs) == 1, "первая попытка ничего не отправляла"


async def _amount():
    from sniperbot.utils.fmt import to_wei

    return to_wei(1000)


async def _huge():
    return 2**255
