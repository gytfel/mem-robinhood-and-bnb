"""Разбор логов PairCreated и дедупликация пар."""

from __future__ import annotations

from hexbytes import HexBytes

from sniperbot.chain.abi import PAIR_CREATED_TOPIC
from sniperbot.chain.clients import ChainClient
from sniperbot.config import ChainConfig, RouterConfig
from sniperbot.sniper.scanner import PairEvent, PairScanner

WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
TOKEN = "0x55d398326f99059fF775485246999027B3197955"
OTHER = "0x1111111111111111111111111111111111111111"
PAIR = "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"
ROUTER = RouterConfig("Test DEX", "0xR" + "0" * 39, "0xF" + "0" * 39, 25, True)


def topic(address: str) -> HexBytes:
    return HexBytes(bytes(12) + bytes.fromhex(address[2:]))


def log_entry(token0: str, token1: str, pair: str, block: int = 100) -> dict:
    return {
        "topics": [HexBytes(PAIR_CREATED_TOPIC), topic(token0), topic(token1)],
        "data": HexBytes(bytes(12) + bytes.fromhex(pair[2:]) + (1).to_bytes(32, "big")),
        "blockNumber": block,
    }


class FakeClient(ChainClient):
    def __init__(self, logs: list[dict]) -> None:
        config = ChainConfig(
            key="bsc", name="Test", chain_id=56, rpc_urls=["http://localhost"],
            wrapped_native=WNATIVE, routers=[ROUTER],
        )
        super().__init__(config)
        self._logs = logs

    async def get_logs(self, params):
        return self._logs

    async def block_number(self):
        return 100


async def collect(events: list):
    async def handler(event: PairEvent) -> None:
        events.append(event)

    return handler


async def test_parses_pair_with_native_token():
    events: list[PairEvent] = []
    scanner = PairScanner(FakeClient([log_entry(WNATIVE, TOKEN, PAIR)]), ROUTER, await collect(events))
    found = await scanner._fetch(1, 100)
    assert len(found) == 1
    assert found[0].token.lower() == TOKEN.lower()
    assert found[0].pair.lower() == PAIR.lower()
    assert found[0].block == 100


async def test_token_order_does_not_matter():
    scanner = PairScanner(FakeClient([log_entry(TOKEN, WNATIVE, PAIR)]), ROUTER, await collect([]))
    found = await scanner._fetch(1, 100)
    assert found[0].token.lower() == TOKEN.lower()
    assert found[0].quote.lower() == WNATIVE.lower()


async def test_pairs_without_native_are_skipped():
    scanner = PairScanner(FakeClient([log_entry(TOKEN, OTHER, PAIR)]), ROUTER, await collect([]))
    assert await scanner._fetch(1, 100) == []


async def test_broken_log_does_not_break_batch():
    logs = [{"topics": [HexBytes(PAIR_CREATED_TOPIC)], "data": HexBytes(b""), "blockNumber": 1},
            log_entry(WNATIVE, TOKEN, PAIR)]
    scanner = PairScanner(FakeClient(logs), ROUTER, await collect([]))
    assert len(await scanner._fetch(1, 100)) == 1


async def test_dispatch_deduplicates(db):
    events: list[PairEvent] = []
    scanner = PairScanner(FakeClient([]), ROUTER, await collect(events))
    event = PairEvent(chain="bsc", pair=PAIR, token=TOKEN, quote=WNATIVE, block=100, router=ROUTER)

    await scanner._dispatch(event)
    await scanner._dispatch(event)

    assert len(events) == 1
    assert events[0].pair_id is not None


async def test_cursor_is_persisted(db):
    scanner = PairScanner(FakeClient([]), ROUTER, await collect([]))
    assert await scanner._load_cursor() == 0
    await scanner._save_cursor(12345)
    assert await scanner._load_cursor() == 12345
