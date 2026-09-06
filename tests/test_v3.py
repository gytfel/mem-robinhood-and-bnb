"""Uniswap V3: пулы, котировки, кодировка свапов и симуляция honeypot."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from eth_abi import encode as abi_encode

from sniperbot.chain.clients import ChainClient
from sniperbot.chain.dex_adapter import PoolRef, V3Adapter, sqrt_price_to_native
from sniperbot.config import ChainConfig, RouterConfig
from sniperbot.sniper.safety import HoneypotSimulator
from sniperbot.utils.evm import mapping_slot, nested_mapping_slot

ROUTER = "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45"
FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
QUOTER = "0x61fFE014bA17989E743c5F6cB21bF9697530B21e"
WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
TOKEN = "0x55d398326f99059fF775485246999027B3197955"
POOLS = {
    500: "0x1111111111111111111111111111111111111111",
    3000: "0x2222222222222222222222222222222222222222",
}
DEPTH = {POOLS[500]: 5 * 10**18, POOLS[3000]: 40 * 10**18}   # в пуле 0.3% ликвидности больше
RATE = 1000  # 1 нативная монета = 1000 токенов
PROBE_VALUE = 0x1234567890


class Revert(Exception):
    pass


ROUTER02_FIELDS = ("tokenIn", "tokenOut", "fee", "recipient", "amountIn",
                   "amountOutMinimum", "sqrtPriceLimitX96")
ROUTER01_FIELDS = ("tokenIn", "tokenOut", "fee", "recipient", "deadline", "amountIn",
                   "amountOutMinimum", "sqrtPriceLimitX96")


def _fields(params, variant: str = "router02") -> dict:
    """web3 отдаёт структуру то словарём, то кортежем — приводим к словарю."""
    if isinstance(params, dict):
        return params
    names = ROUTER02_FIELDS if variant == "router02" else ROUTER01_FIELDS
    return dict(zip(names, params, strict=False))


class FakeV3Client(ChainClient):
    """Нода с одним V3-пулом: считает выход по курсу и удерживает налоги."""

    def __init__(self, *, buy_tax_bps=0, sell_tax_bps=0, sellable=True, buyable=True,
                 accepts="router02", quoter_v2=True, balance_slot=3, allowance_slot=4,
                 pools=None):
        config = ChainConfig(
            key="test", name="Test", chain_id=1, native_symbol="TST",
            rpc_urls=["http://localhost:8545"], wrapped_native=WNATIVE,
            routers=[RouterConfig("Uniswap V3", ROUTER, FACTORY, 30, True,
                                  kind="v3", quoter=QUOTER, fee_tiers=(500, 3000))],
        )
        super().__init__(config)
        self.buy_tax_bps = buy_tax_bps
        self.sell_tax_bps = sell_tax_bps
        self.sellable = sellable
        self.buyable = buyable
        self.accepts = accepts
        self.quoter_v2 = quoter_v2
        self.balance_slot = balance_slot
        self.allowance_slot = allowance_slot
        self.pools = POOLS if pools is None else pools
        self.quoter_calls: list[str] = []

    # ------------------------------------------------------------- view-вызовы
    async def call(self, address, abi, fn_name, *args, **kwargs):
        address = address.lower()
        if fn_name == "getPool":
            fee = args[2]
            return self.pools.get(fee, "0x" + "0" * 40)
        if fn_name == "balanceOf":
            holder = args[0]
            if address == WNATIVE.lower():
                return DEPTH.get(holder, 0)
            return 10**24
        if fn_name == "slot0":
            # 1 токен = 0.001 нативной монеты, токен = token0
            sqrt_price = int(math.sqrt(0.001) * 2**96)
            return [sqrt_price, 0, 0, 0, 0, 0, True]
        if fn_name == "token0":
            return TOKEN
        if fn_name == "quoteExactInputSingle":
            return self._quote(args)
        raise AssertionError(f"неожиданный вызов {fn_name}")

    def _quote(self, args):
        if len(args) == 1:            # QuoterV2: одна структура
            self.quoter_calls.append("v2")
            if not self.quoter_v2:
                raise Revert("нет такого метода")
            token_in, _token_out, amount_in, _fee, _limit = args[0]
            return [self._out(token_in, amount_in), 0, 0, 0]
        self.quoter_calls.append("v1")   # Quoter v1: пять аргументов
        token_in, _token_out, _fee, amount_in, _limit = args
        return self._out(token_in, amount_in)

    def _out(self, token_in: str, amount_in: int) -> int:
        if token_in.lower() == WNATIVE.lower():
            return amount_in * RATE
        return amount_in // RATE

    # --------------------------------------------------------------- eth_call
    async def raw_call(self, tx, state_override=None, block="latest"):
        to = (tx.get("to") or "").lower()
        data = tx["data"]
        if to == WNATIVE.lower():
            return abi_encode(["uint256"], [10**24])
        if to == TOKEN.lower():
            return self._token_call(data, state_override or {})
        if to == ROUTER.lower():
            return self._swap(tx, data)
        raise AssertionError(f"неожиданный адрес {to}")

    def _token_call(self, data: str, overrides: dict) -> bytes:
        diff = {}
        for value in overrides.values():
            diff.update(value.get("stateDiff") or {})
        selector, holder = data[:10], "0x" + data[34:74]
        if selector == "0x70a08231":
            key = mapping_slot(holder, self.balance_slot)
        elif selector == "0xdd62ed3e":
            key = nested_mapping_slot(holder, "0x" + data[98:138], self.allowance_slot)
        else:
            raise AssertionError(f"неожиданный вызов токена {selector}")
        return bytes.fromhex(diff.get(key, "0x" + "0" * 64)[2:])

    def _swap(self, tx: dict, data: str) -> bytes:
        from sniperbot.chain.abi import V3_ROUTER01_ABI, V3_ROUTER02_ABI

        abi = V3_ROUTER02_ABI if self.accepts == "router02" else V3_ROUTER01_ABI
        contract = self.w3.eth.contract(address=ROUTER, abi=abi)
        try:
            _fn, args = contract.decode_function_input(data)
        except Exception as exc:  # noqa: BLE001 - роутер не понял чужую кодировку
            raise Revert("неизвестный селектор") from exc

        params = _fields(args["params"], self.accepts)
        token_in = params["tokenIn"]
        amount_in = params["amountIn"]
        min_out = params["amountOutMinimum"]

        buying = token_in.lower() == WNATIVE.lower()
        if buying and not self.buyable:
            raise Revert("trading closed")
        if not buying and not self.sellable:
            raise Revert("honeypot")

        tax = self.buy_tax_bps if buying else self.sell_tax_bps
        actual = self._out(token_in, amount_in) * (10_000 - tax) // 10_000
        if min_out > actual:
            raise Revert("Too little received")
        return abi_encode(["uint256"], [actual])


def make_adapter(**kwargs) -> tuple[V3Adapter, FakeV3Client]:
    client = FakeV3Client(**kwargs)
    return V3Adapter(client, client.config.routers[0]), client


@pytest.fixture(autouse=True)
def _clear_caches():
    from sniperbot.chain import dex_adapter
    from sniperbot.sniper import safety

    dex_adapter._variant_cache.clear()
    dex_adapter._quoter_cache.clear()
    safety._slot_cache.clear()
    yield
    dex_adapter._variant_cache.clear()
    dex_adapter._quoter_cache.clear()
    safety._slot_cache.clear()


# ------------------------------------------------------------------ цена из slot0
def test_sqrt_price_converts_to_native_price():
    sqrt_price = int(math.sqrt(0.0001) * 2**96)
    assert float(sqrt_price_to_native(sqrt_price, True, 18)) == pytest.approx(0.0001, rel=1e-9)
    # тот же пул, но токен — token1: цена переворачивается
    assert float(sqrt_price_to_native(sqrt_price, False, 18)) == pytest.approx(10_000, rel=1e-9)


def test_sqrt_price_respects_token_decimals():
    sqrt_price = int(math.sqrt(2 * 10**12) * 2**96)   # 2 native за токен с 6 знаками
    assert float(sqrt_price_to_native(sqrt_price, True, 6)) == pytest.approx(2, rel=1e-9)


def test_sqrt_price_zero_is_safe():
    assert sqrt_price_to_native(0, True, 18) == 0


# ------------------------------------------------------------------------ пулы
async def test_find_pool_picks_deepest_fee_tier():
    adapter, _ = make_adapter()
    pool = await adapter.find_pool(TOKEN)
    assert pool.address == POOLS[3000]
    assert pool.fee == 3000
    assert pool.kind == "v3"
    assert pool.label == "V3 0.3%"


async def test_find_pool_returns_none_without_pools():
    adapter, _ = make_adapter(pools={})
    assert await adapter.find_pool(TOKEN) is None


async def test_pool_state_reads_liquidity_and_price():
    adapter, _ = make_adapter()
    state = await adapter.pool_state(TOKEN, PoolRef(POOLS[3000], "v3", 3000), 18)
    assert state.has_liquidity is True
    assert state.liquidity_native == Decimal(40)
    assert float(state.price_native) == pytest.approx(0.001, rel=1e-6)


# ------------------------------------------------------------------ котировки
async def test_quote_uses_quoter_v2():
    adapter, client = make_adapter()
    pool = PoolRef(POOLS[3000], "v3", 3000)
    assert await adapter.quote_buy(TOKEN, 10**18, pool) == 10**18 * RATE
    assert await adapter.quote_sell(TOKEN, 10**18 * RATE, pool) == 10**18
    assert client.quoter_calls[0] == "v2"


async def test_quote_falls_back_to_quoter_v1():
    adapter, client = make_adapter(quoter_v2=False)
    amount = await adapter.quote_buy(TOKEN, 10**18, PoolRef(POOLS[3000], "v3", 3000))
    assert amount == 10**18 * RATE
    assert client.quoter_calls == ["v2", "v1"]


async def test_quote_without_quoter_is_reported():
    from sniperbot.chain.dex import DexError

    client = FakeV3Client()
    cfg = RouterConfig("V3", ROUTER, FACTORY, 30, True, kind="v3", quoter="")
    adapter = V3Adapter(client, cfg)
    with pytest.raises(DexError, match="Quoter"):
        await adapter.quote_buy(TOKEN, 10**18, PoolRef(POOLS[3000], "v3", 3000))


# ------------------------------------------------------------------ кодировка
def test_encode_buy_matches_router02_layout():
    from sniperbot.chain.abi import V3_ROUTER02_ABI

    adapter, client = make_adapter()
    data = adapter.encode_buy(TOKEN, 10**18, 42, TOKEN, PoolRef(POOLS[3000], "v3", 3000))
    contract = client.w3.eth.contract(address=ROUTER, abi=V3_ROUTER02_ABI)
    _fn, args = contract.decode_function_input(data)
    params = _fields(args["params"])
    assert params["tokenIn"].lower() == WNATIVE.lower()
    assert params["tokenOut"].lower() == TOKEN.lower()
    assert params["fee"] == 3000
    assert params["amountIn"] == 10**18
    assert params["amountOutMinimum"] == 42
    assert "deadline" not in params                  # SwapRouter02 без deadline


def test_router01_layout_has_deadline():
    from sniperbot.chain.abi import V3_ROUTER01_ABI

    adapter, client = make_adapter()
    adapter.variant = "router01"
    data = adapter.encode_sell(TOKEN, 10**18, 7, TOKEN, PoolRef(POOLS[3000], "v3", 3000))
    contract = client.w3.eth.contract(address=ROUTER, abi=V3_ROUTER01_ABI)
    _fn, args = contract.decode_function_input(data)
    params = _fields(args["params"], "router01")
    assert params["deadline"] > 0
    assert params["amountOutMinimum"] == 7
    assert params["amountIn"] == 10**18


def test_variant_switch_is_one_way_and_lockable():
    adapter, _ = make_adapter()
    assert adapter.variant == "router02"
    assert adapter.try_next_variant() is True
    assert adapter.variant == "router01"
    assert adapter.try_next_variant() is False       # других вариантов нет

    client = FakeV3Client()
    locked = V3Adapter(client, RouterConfig("V3", ROUTER, FACTORY, 30, True, kind="v3",
                                            quoter=QUOTER, variant="router02"))
    assert locked.try_next_variant() is False        # вариант зафиксирован конфигом


# ------------------------------------------------------------------ симуляция
def make_simulator(**kwargs) -> HoneypotSimulator:
    adapter, client = make_adapter(**kwargs)
    return HoneypotSimulator(client, adapter, PoolRef(POOLS[3000], "v3", 3000))


async def test_v3_clean_token_passes():
    result = await make_simulator().simulate(TOKEN, 18, 10**16)
    assert result.available is True
    assert result.can_buy is True
    assert result.can_sell is True
    assert result.buy_tax_bps == 0
    assert result.sell_tax_bps == 0


async def test_v3_taxes_are_measured():
    result = await make_simulator(buy_tax_bps=800, sell_tax_bps=1200).simulate(TOKEN, 18, 10**16)
    assert result.buy_tax_bps == 800
    assert result.sell_tax_bps == 1200


async def test_v3_honeypot_detected():
    result = await make_simulator(sellable=False).simulate(TOKEN, 18, 10**16)
    assert result.can_buy is True
    assert result.can_sell is False
    assert result.is_honeypot is True


async def test_v3_simulation_detects_router_variant():
    """Роутер понимает только старую кодировку — симулятор сам переключается."""
    simulator = make_simulator(accepts="router01")
    result = await simulator.simulate(TOKEN, 18, 10**16)
    assert result.can_buy is True
    assert result.can_sell is True
    assert simulator.adapter.variant == "router01"

    from sniperbot.chain import dex_adapter

    assert dex_adapter._variant_cache[ROUTER.lower()] == "router01"
