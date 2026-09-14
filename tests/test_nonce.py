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
