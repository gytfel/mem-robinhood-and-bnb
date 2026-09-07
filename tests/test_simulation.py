"""Тесты симулятора honeypot/налогов на поддельной ноде."""

from __future__ import annotations

import pytest
from eth_abi import encode as abi_encode

from sniperbot.chain.clients import ChainClient
from sniperbot.chain.dex_adapter import PoolRef, V2Adapter
from sniperbot.config import ChainConfig, RouterConfig
from sniperbot.sniper.safety import HoneypotSimulator, _tax_bps
from sniperbot.utils.evm import hex32, mapping_slot, nested_mapping_slot

ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
TOKEN = "0x55d398326f99059fF775485246999027B3197955"
PAIR = "0x16b9a82891338f9bA80E2D6970FddA79D1eb0daE"
RATE = 1000  # 1 нативная монета = 1000 токенов
PROBE_VALUE = 0x1234567890


class Revert(Exception):
    pass


class FakeClient(ChainClient):
    """Клиент, отвечающий на eth_call без сети: имитирует пул и токен."""

    def __init__(self, *, buy_tax_bps=0, sell_tax_bps=0, sellable=True, buyable=True,
                 balance_slot=3, allowance_slot=4, supports_override=True):
        config = ChainConfig(
            key="test", name="Test", chain_id=1, native_symbol="TST",
            rpc_urls=["http://localhost:8545"], wrapped_native=WNATIVE,
            routers=[RouterConfig("Test DEX", ROUTER, FACTORY, 25, True)],
        )
        super().__init__(config)
        self.buy_tax_bps = buy_tax_bps
        self.sell_tax_bps = sell_tax_bps
        self.sellable = sellable
        self.buyable = buyable
        self.balance_slot = balance_slot
        self.allowance_slot = allowance_slot
        self.supports_state_override = supports_override
        self.calls = 0

    async def call(self, address, abi, fn_name, *args, **kwargs):
        if fn_name == "getAmountsOut":
            amount, path = args
            if path[-1].lower() == TOKEN.lower():
                return [amount, amount * RATE]
            return [amount, amount // RATE]
        raise AssertionError(f"неожиданный вызов {fn_name}")

    async def raw_call(self, tx, state_override=None, block="latest"):
        self.calls += 1
        if not self.supports_state_override and state_override:
            raise ValueError("this node does not support state override")

        to = (tx.get("to") or "").lower()
        data = tx["data"]
        if to == WNATIVE.lower():
            return abi_encode(["uint256"], [10**24])   # totalSupply при проверке override
        if to == TOKEN.lower():
            return self._token_call(data, state_override or {})
        if to == ROUTER.lower():
            return self._router_call(tx, data)
        raise AssertionError(f"неожиданный адрес {to}")

    # ------------------------------------------------------------- «токен»
    def _token_call(self, data: str, overrides: dict) -> bytes:
        diff = {}
        for value in overrides.values():
            diff.update(value.get("stateDiff") or {})
        selector = data[:10]
        holder = "0x" + data[34:74]
        if selector == "0x70a08231":  # balanceOf(address)
            key = mapping_slot(holder, self.balance_slot)
            return bytes.fromhex(diff.get(key, hex32(0))[2:])
        if selector == "0xdd62ed3e":  # allowance(address,address)
            spender = "0x" + data[98:138]
            key = nested_mapping_slot(holder, spender, self.allowance_slot)
            return bytes.fromhex(diff.get(key, hex32(0))[2:])
        raise AssertionError(f"неожиданный вызов токена {selector}")

    # ------------------------------------------------------------ «роутер»
    def _router_call(self, tx: dict, data: str) -> bytes:
        contract = self.router(ROUTER)
        fn, args = contract.decode_function_input(data)
        name = fn.fn_name
        if name == "WETH":
            return abi_encode(["address"], [WNATIVE])
        if name == "swapExactETHForTokensSupportingFeeOnTransferTokens":
            if not self.buyable:
                raise Revert("trading closed")
            value = int(tx["value"], 16) if isinstance(tx["value"], str) else int(tx["value"])
            expected = value * RATE
            actual = expected * (10_000 - self.buy_tax_bps) // 10_000
            if args["amountOutMin"] > actual:
                raise Revert("INSUFFICIENT_OUTPUT_AMOUNT")
            return b""
        if name == "swapExactTokensForETHSupportingFeeOnTransferTokens":
            if not self.sellable:
                raise Revert("honeypot")
            expected = args["amountIn"] // RATE
            actual = expected * (10_000 - self.sell_tax_bps) // 10_000
            if args["amountOutMin"] > actual:
                raise Revert("INSUFFICIENT_OUTPUT_AMOUNT")
            return b""
        raise AssertionError(f"неожиданная функция {name}")


@pytest.fixture(autouse=True)
def _clear_slot_cache():
    """Слоты кешируются по (сеть, токен) — между тестами кеш надо чистить."""
    from sniperbot.sniper import safety

    safety._slot_cache.clear()
    yield
    safety._slot_cache.clear()


def make_simulator(**kwargs) -> HoneypotSimulator:
    client = FakeClient(**kwargs)
    adapter = V2Adapter(client, client.config.routers[0])
    return HoneypotSimulator(client, adapter, PoolRef(address=PAIR, kind="v2"))


async def test_clean_token_passes():
    sim = make_simulator()
    result = await sim.simulate(TOKEN, 18, 10**16)
    assert result.available is True
    assert result.can_buy is True
    assert result.can_sell is True
    assert result.buy_tax_bps == 0
    assert result.sell_tax_bps == 0


async def test_taxes_are_measured():
    sim = make_simulator(buy_tax_bps=1000, sell_tax_bps=1500)
    result = await sim.simulate(TOKEN, 18, 10**16)
    # погрешность двоичного поиска — доли процента
    assert result.buy_tax_bps == 1000
    assert result.sell_tax_bps == 1500


async def test_honeypot_detected():
    sim = make_simulator(sellable=False)
    result = await sim.simulate(TOKEN, 18, 10**16)
    assert result.can_buy is True
    assert result.can_sell is False
    assert result.is_honeypot is True


async def test_unbuyable_token_detected():
    sim = make_simulator(buyable=False)
    result = await sim.simulate(TOKEN, 18, 10**16)
    assert result.can_buy is False
    assert result.is_honeypot is True


async def test_missing_balance_slot_leaves_sell_unknown():
    sim = make_simulator(balance_slot=99)  # слот вне диапазона перебора
    result = await sim.simulate(TOKEN, 18, 10**16)
    assert result.can_buy is True
    assert result.can_sell is None
    assert "слот" in (result.error or "")


async def test_node_without_override_degrades_gracefully():
    sim = make_simulator(supports_override=False)
    result = await sim.simulate(TOKEN, 18, 10**16)
    assert result.available is False
    assert result.can_sell is None


@pytest.mark.parametrize(
    ("expected", "actual", "bps"),
    [(1000, 1000, 0), (1000, 900, 1000), (1000, 0, 10_000), (0, 0, 0), (10_000, 9_999, 0)],
)
def test_tax_bps(expected, actual, bps):
    assert _tax_bps(expected, actual) == bps


# ------------------------------------------- скорость измерения налога
async def test_min_out_search_is_parallel_and_accurate():
    """Пробы идут пачками: на медленной ноде это главный источник задержки."""
    import asyncio

    from sniperbot.sniper.safety import SEARCH_PROBES, SEARCH_ROUNDS

    simulator = make_simulator()
    threshold = 8_123_456              # всё, что выше, не проходит
    state = {"active": 0, "peak": 0, "calls": 0, "waves": 0}

    async def fake_call(tx, overrides):
        state["calls"] += 1
        state["active"] += 1
        state["peak"] = max(state["peak"], state["active"])
        await asyncio.sleep(0)          # даём другим пробам стартовать
        state["active"] -= 1
        value = int(tx["data"])
        return None if value > threshold else b"ok"

    simulator._call = fake_call         # type: ignore[method-assign]
    best = await simulator._max_passing_min_out({}, {}, lambda value: str(value), 10_000_000)

    assert best <= threshold                       # никогда не завышаем
    assert threshold - best < threshold * 0.01     # точность лучше 1%
    assert state["peak"] > 1                       # пробы шли параллельно
    assert state["calls"] <= SEARCH_PROBES * SEARCH_ROUNDS


async def test_min_out_search_handles_hopeless_case():
    """Если не проходит даже минимум — возвращаем ноль, а не зависаем."""
    simulator = make_simulator()

    async def always_revert(tx, overrides):
        return None

    simulator._call = always_revert     # type: ignore[method-assign]
    assert await simulator._max_passing_min_out({}, {}, lambda value: str(value), 1_000) == 0
    assert await simulator._max_passing_min_out({}, {}, lambda value: str(value), 0) == 0
