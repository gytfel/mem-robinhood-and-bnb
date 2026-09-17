"""Поток секвенсора: разбор кадров и пробуждение бота.

Поток — это только звонок будильника. Он не решает, что покупать, и всё, что
из него приходит, бот всё равно перепроверяет через RPC. Поэтому проверять
здесь надо две вещи: правильно ли разобран кадр и будит ли он ровно тогда,
когда тронули наши адреса.
"""

from __future__ import annotations

import base64
import json

import pytest
from eth_account import Account

from sniperbot.chain.feed import (
    KIND_BATCH,
    KIND_SIGNED_TX,
    SequencerFeed,
    iter_transactions,
    parse_frame,
    targets,
    tx_target,
)

KEY = "0x" + "7" * 64
ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
STRANGER = "0x55d398326f99059fF775485246999027B3197955"

BASE = {"nonce": 5, "gas": 200_000, "value": 10**16, "chainId": 4663, "data": b"\xab\xcd"}


def signed(to: str | None = ROUTER, kind: str = "1559") -> bytes:
    """Настоящая подписанная транзакция — разбор проверяем на них, не на макетах."""
    tx = dict(BASE)
    if kind == "legacy":
        tx["gasPrice"] = 10**9
    elif kind == "2930":
        tx.update(type=1, gasPrice=10**9, accessList=[])
    else:
        tx.update(type=2, maxFeePerGas=10**9, maxPriorityFeePerGas=10**8)
    if to is not None:
        tx["to"] = to
    return bytes(Account.sign_transaction(tx, KEY).raw_transaction)


def frame(*messages: bytes, sequence: int = 1234) -> str:
    """Кадр в том виде, в каком его шлёт релей Nitro."""
    return json.dumps({"version": 1, "messages": [
        {
            "sequenceNumber": sequence + index,
            "message": {"message": {"header": {"kind": 3}, "l2Msg":
                                    base64.b64encode(body).decode()},
                        "delayedMessagesRead": 0},
            "signature": None,
        }
        for index, body in enumerate(messages)
    ]})


def one(raw: bytes) -> bytes:
    return bytes([KIND_SIGNED_TX]) + raw


def batch(*raws: bytes) -> bytes:
    body = b"".join(len(one(raw)).to_bytes(8, "big") + one(raw) for raw in raws)
    return bytes([KIND_BATCH]) + body


# --------------------------------------------------------- адрес получателя
@pytest.mark.parametrize("kind", ["legacy", "2930", "1559"])
def test_the_recipient_is_read_from_every_transaction_type(kind):
    assert tx_target(signed(ROUTER, kind)) == bytes.fromhex(ROUTER[2:])


def test_a_contract_deployment_has_no_recipient():
    assert tx_target(signed(None)) is None


def test_a_long_calldata_is_handled():
    """Длинная data меняет заголовок RLP — на этом ломаются наивные разборы."""
    tx = {**BASE, "type": 2, "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8,
          "to": ROUTER, "data": b"\x11" * 5000}
    raw = bytes(Account.sign_transaction(tx, KEY).raw_transaction)
    assert tx_target(raw) == bytes.fromhex(ROUTER[2:])


@pytest.mark.parametrize("raw", [b"", b"\x05abc", b"\xff", b"\x02\xc0", b"\x02"])
def test_garbage_does_not_break_the_reader(raw):
    assert tx_target(raw) is None


# ------------------------------------------------------------ разбор кадра
def test_a_frame_yields_its_messages():
    found = parse_frame(frame(one(signed()), one(signed(STRANGER))))
    assert [number for number, _ in found] == [1234, 1235]


@pytest.mark.parametrize("payload", ["", "не json", "{}", '{"messages": null}', b"\x00\x01"])
def test_a_broken_frame_is_skipped(payload):
    assert parse_frame(payload) == []


def test_transactions_are_unpacked_from_a_batch():
    found = list(iter_transactions(batch(signed(ROUTER), signed(STRANGER))))
    assert len(found) == 2
    assert {tx_target(raw) for raw in found} == {bytes.fromhex(ROUTER[2:]),
                                                bytes.fromhex(STRANGER[2:])}


def test_a_truncated_batch_stops_quietly():
    broken = bytes([KIND_BATCH]) + (999).to_bytes(8, "big") + b"\x04\x01\x02"
    assert list(iter_transactions(broken)) == []


def test_service_messages_are_ignored():
    assert list(iter_transactions(bytes([0]) + b"whatever")) == []
    assert list(iter_transactions(b"")) == []


def test_nesting_has_a_limit():
    """Пачка в пачке в пачке — защита от кадра, который зациклит разбор."""
    deep = one(signed())
    for _ in range(10):
        deep = bytes([KIND_BATCH]) + len(deep).to_bytes(8, "big") + deep
    assert list(iter_transactions(deep)) == []


# ----------------------------------------------------------- пробуждение
def feed_for(watched: set[str]) -> tuple[SequencerFeed, list]:
    woken: list[int] = []
    return SequencerFeed("wss://feed.example", watched, woken.append, name="тест"), woken


async def test_our_router_wakes_the_bot():
    feed, woken = feed_for({ROUTER})
    assert await feed.handle(frame(one(signed(ROUTER)))) is True
    assert woken == [1234]
    assert feed.hits == 1


async def test_someone_elses_transaction_is_ignored():
    """Иначе бот будил бы сканер на каждый чужой перевод — десятки раз в секунду."""
    feed, woken = feed_for({ROUTER})
    assert await feed.handle(frame(one(signed(STRANGER)))) is False
    assert woken == []


async def test_a_batch_with_our_address_inside_wakes_the_bot():
    feed, woken = feed_for({FACTORY})
    assert await feed.handle(frame(batch(signed(STRANGER), signed(FACTORY)))) is True
    assert woken


async def test_the_block_number_is_remembered():
    feed, _ = feed_for({ROUTER})
    await feed.handle(frame(one(signed(ROUTER)), sequence=9000))
    assert feed.last_sequence == 9000


async def test_a_broken_handler_does_not_kill_the_subscription():
    def explode(_sequence):
        raise RuntimeError("обработчик упал")

    feed = SequencerFeed("wss://feed.example", {ROUTER}, explode)
    assert await feed.handle(frame(one(signed(ROUTER)))) is True, "подписка обязана выжить"


async def test_without_watched_addresses_there_is_nothing_to_do():
    feed, woken = feed_for(set())
    await feed.run()          # мгновенно возвращается, соединение не открывает
    assert await feed.handle(frame(one(signed(ROUTER)))) is False
    assert woken == []


def test_targets_collects_every_recipient():
    assert targets(batch(signed(ROUTER), signed(STRANGER))) == {
        bytes.fromhex(ROUTER[2:]), bytes.fromhex(STRANGER[2:])
    }


# ------------------------------------------- будильник доходит до сканера
async def test_the_scanner_wakes_up_early():
    """Иначе пара, созданная сразу после опроса, ждала бы полный интервал."""
    import time

    from sniperbot.config import ChainConfig, RouterConfig
    from sniperbot.sniper.scanner import PairScanner

    chain = ChainConfig(key="rh", name="RH", chain_id=4663, rpc_urls=["http://localhost"],
                        wrapped_native="0x" + "b" * 40,
                        routers=[RouterConfig("DEX", ROUTER, FACTORY, 25, True)])
    scanner = PairScanner(client=None, router_cfg=chain.routers[0], handler=None,
                          poll_interval=30.0)

    started = time.monotonic()
    scanner.wake()
    await scanner._pause(30.0)

    assert time.monotonic() - started < 1.0, "звонок будильника должен прерывать ожидание"


async def test_without_a_call_the_scanner_keeps_its_own_pace():
    import time

    from sniperbot.config import RouterConfig
    from sniperbot.sniper.scanner import PairScanner

    scanner = PairScanner(client=None, router_cfg=RouterConfig("DEX", ROUTER, FACTORY, 25, True),
                          handler=None, poll_interval=0.05)
    started = time.monotonic()
    await scanner._pause(0.05)
    assert time.monotonic() - started >= 0.04


def test_the_feed_starts_only_when_the_chain_gives_one(monkeypatch):
    """Нет адреса потока — нет подписки: для BSC его не существует."""
    from sniperbot.config import ChainConfig, RouterConfig
    from sniperbot.sniper import engine as engine_module

    chain = ChainConfig(key="rh", name="RH", chain_id=4663, rpc_urls=["http://localhost"],
                        wrapped_native="0x" + "b" * 40,
                        routers=[RouterConfig("DEX", ROUTER, FACTORY, 25, True)])
    instance = engine_module.SniperEngine.__new__(engine_module.SniperEngine)
    instance.feeds = {}
    instance._tasks = []

    instance._start_feed("rh", chain, [])
    assert instance.feeds == {}

    chain.feed_url = "wss://feed.mainnet.chain.robinhood.com"
    created: list = []

    class FakeFeed:
        def __init__(self, url, watched, poke, name=""):  # noqa: ANN001
            created.append((url, watched))
            self.poke = poke

        async def run(self):
            return None

    monkeypatch.setattr(engine_module, "SequencerFeed", FakeFeed)
    monkeypatch.setattr(engine_module.asyncio, "create_task",
                        lambda coro, name="": coro.close())

    instance._start_feed("rh", chain, [])

    assert created and created[0][0] == "wss://feed.mainnet.chain.robinhood.com"
    assert created[0][1] == {ROUTER, FACTORY}, "следим за роутером и фабрикой сети"


# ------------------------------------------------- рукопожатие со сжатием
def test_compression_is_offered_by_default():
    """Ленты Nitro переходят на обязательное сжатие: клиент без него получает отказ."""
    from sniperbot.chain.feed import COMPRESS_BITS

    feed = SequencerFeed("wss://feed.example", {ROUTER}, lambda _: None)
    assert feed.compress == COMPRESS_BITS
    assert feed.headers == {"Arbitrum-Feed-Client-Version": "2"}


def test_the_handshake_can_be_changed_without_touching_the_code():
    """Если у сети свои требования, их должно хватить передать снаружи."""
    feed = SequencerFeed("wss://feed.example", {ROUTER}, lambda _: None,
                         compress=0, headers={"X-Key": "секрет"})
    assert feed.compress == 0
    assert feed.headers == {"X-Key": "секрет"}


def test_every_probe_variant_is_distinct():
    """Перебор должен покрывать сжатие, заголовки и путь — без повторов."""
    from sniperbot.chain.feed import PROBE_VARIANTS

    shapes = {(compress, headers, path) for _title, compress, headers, path in PROBE_VARIANTS}
    assert len(shapes) == len(PROBE_VARIANTS), "повторяющийся вариант — потраченная попытка"
    assert any(path == "/feed" for _t, _c, _h, path in PROBE_VARIANTS)
    assert any(compress for _t, compress, _h, _p in PROBE_VARIANTS)
    assert any(not compress for _t, compress, _h, _p in PROBE_VARIANTS)
    assert any("User-Agent" in dict(headers) for _t, _c, headers, _p in PROBE_VARIANTS), \
        "заслон, который не пускает не-браузеры, — самый частый случай"


# ------------------------------------------------- адрес узла до подключения
@pytest.mark.parametrize("url", [
    "wss://api-robinhood-mainnet.n.dwellir.com/9f3c-abcdef",
    "wss://rpc.ordofi.network",
    "ws://127.0.0.1:9642",
    "wss://cold-example-key.quiknode.pro/3c1ae59d1c5c67ffc32ab3ba40faf71504a63181/",
])
def test_a_real_address_passes(url):
    """Ключ в пути может содержать любые буквы — придираться к нему нельзя."""
    from sniperbot.chain.feed import url_problem

    assert url_problem(url) == ""


@pytest.mark.parametrize("url,hint", [
    ("wss://ваш-адрес-с-ключом", "заглушка"),
    ("wss://<your-key>.example.com", "пример"),
    ("https://rpc.example.com", "wss://"),
    ("rpc.example.com", "wss://"),
    ("", "пустой"),
    ("wss://", "нет имени сервера"),
    ("wss://rpc.example.com/ключ сюда", "пробел"),
])
def test_a_placeholder_is_called_out_before_connecting(url, hint):
    """Иначе человек видит ошибку DNS и думает, что дело в ключе."""
    from sniperbot.chain.feed import url_problem

    problem = url_problem(url)
    assert problem and hint in problem
