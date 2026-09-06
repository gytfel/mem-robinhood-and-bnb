"""Единый интерфейс к DEX: Uniswap V2 и Uniswap V3 за одним API.

Всё остальное приложение (исполнение сделок, мониторинг позиций, проверки
безопасности, сканер) работает через :class:`DexAdapter` и не знает, какая
версия протокола под ним.

Различия, которые прячет адаптер:

* **V2** — пара `token/WNATIVE`, резервы, `getAmountsOut`, свапы с поддержкой
  токенов с комиссией на перевод; продажа сразу отдаёт нативную монету.
* **V3** — пул на конкретном тире комиссии (100/500/3000/10000), цена из
  `slot0().sqrtPriceX96`, котировки через Quoter, свап `exactInputSingle`;
  продажа отдаёт WETH, который затем разворачивается в нативную монету.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sniperbot.chain.abi import (
    ERC20_ABI,
    QUOTER_V1_ABI,
    QUOTER_V2_ABI,
    V3_FACTORY_ABI,
    V3_POOL_ABI,
    V3_ROUTER01_ABI,
    V3_ROUTER02_ABI,
)
from sniperbot.chain.clients import ChainClient
from sniperbot.chain.dex import (
    DexError,
    build_approve_tx,
    build_buy_tx,
    build_sell_tx,
    deadline,
    get_pair_address,
    lp_burned_pct,
    quote_buy,
    quote_sell,
    read_pair,
)
from sniperbot.config import RouterConfig
from sniperbot.utils.evm import ZERO_ADDRESS, to_checksum
from sniperbot.utils.fmt import from_wei

log = logging.getLogger(__name__)

Q96 = Decimal(2) ** 96
MAX_UINT256 = 2**256 - 1

# Какой вариант роутера/квотера реально работает — запоминаем по адресу,
# чтобы не перебирать кодировки на каждой сделке.
_variant_cache: dict[str, str] = {}
_quoter_cache: dict[str, str] = {}


@dataclass(slots=True)
class PoolRef:
    """Ссылка на пул: адрес плюс тир комиссии для V3."""

    address: str
    kind: str = "v2"
    fee: int = 0

    @property
    def label(self) -> str:
        return f"V3 {self.fee / 10_000:g}%" if self.kind == "v3" else "V2"


@dataclass(slots=True)
class PoolState:
    pool: PoolRef
    liquidity_native: Decimal = Decimal(0)
    price_native: Decimal = Decimal(0)
    reserve_native: int = 0
    reserve_token: int = 0
    token_decimals: int = 18
    extra: dict = field(default_factory=dict)

    @property
    def has_liquidity(self) -> bool:
        return self.reserve_native > 0


class DexAdapter:
    """Базовый интерфейс. Реализации ниже."""

    kind = "v2"
    needs_unwrap = False

    def __init__(self, client: ChainClient, cfg: RouterConfig) -> None:
        self.client = client
        self.cfg = cfg
        self.router = to_checksum(cfg.router)
        self.wnative = to_checksum(client.config.wrapped_native)

    @property
    def spender(self) -> str:
        """Кому давать approve перед продажей."""
        return self.router

    @property
    def name(self) -> str:
        return self.cfg.name

    # Ниже — методы, которые реализуют наследники.
    async def find_pool(self, token: str) -> PoolRef | None: ...
    async def pool_state(self, token: str, pool: PoolRef, decimals: int = 18) -> PoolState: ...
    async def quote_buy(self, token: str, amount_native: int, pool: PoolRef) -> int: ...
    async def quote_sell(self, token: str, amount_tokens: int, pool: PoolRef) -> int: ...
    def encode_buy(self, token: str, amount_native: int, min_out: int, recipient: str, pool: PoolRef) -> str: ...
    def encode_sell(self, token: str, amount_tokens: int, min_out: int, recipient: str, pool: PoolRef) -> str: ...
    async def build_buy_tx(self, token, wallet, amount_native, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict: ...
    async def build_sell_tx(self, token, wallet, amount_tokens, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict: ...

    async def build_approve_tx(self, token: str, wallet: str, amount: int, *, nonce: int, gas_fees: dict) -> dict:
        return await build_approve_tx(
            self.client, token, self.spender, wallet, amount, nonce=nonce, gas_fees=gas_fees
        )

    async def lp_burned(self, pool: PoolRef) -> Decimal | None:
        return None

    def try_next_variant(self) -> bool:
        """Переключиться на другую кодировку вызова, если такая есть."""
        return False


class V2Adapter(DexAdapter):
    """Uniswap V2 и совместимые форки (PancakeSwap V2, Biswap, …)."""

    kind = "v2"

    async def find_pool(self, token: str) -> PoolRef | None:
        pair = await get_pair_address(self.client, self.cfg, token)
        return PoolRef(address=pair, kind="v2") if pair else None

    async def pool_state(self, token: str, pool: PoolRef, decimals: int = 18) -> PoolState:
        state = await read_pair(self.client, pool.address, token, decimals)
        return PoolState(
            pool=pool,
            liquidity_native=state.liquidity_native,
            price_native=state.price_native,
            reserve_native=state.reserve_native,
            reserve_token=state.reserve_token,
            token_decimals=decimals,
        )

    async def quote_buy(self, token: str, amount_native: int, pool: PoolRef) -> int:
        return await quote_buy(self.client, self.router, token, amount_native)

    async def quote_sell(self, token: str, amount_tokens: int, pool: PoolRef) -> int:
        return await quote_sell(self.client, self.router, token, amount_tokens)

    def encode_buy(self, token: str, amount_native: int, min_out: int, recipient: str, pool: PoolRef) -> str:
        contract = self.client.router(self.router)
        return contract.encode_abi(
            "swapExactETHForTokensSupportingFeeOnTransferTokens",
            args=[min_out, [self.wnative, to_checksum(token)], to_checksum(recipient), deadline()],
        )

    def encode_sell(self, token: str, amount_tokens: int, min_out: int, recipient: str, pool: PoolRef) -> str:
        contract = self.client.router(self.router)
        return contract.encode_abi(
            "swapExactTokensForETHSupportingFeeOnTransferTokens",
            args=[amount_tokens, min_out, [to_checksum(token), self.wnative], to_checksum(recipient), deadline()],
        )

    async def build_buy_tx(self, token, wallet, amount_native, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict:
        return await build_buy_tx(
            self.client, self.router, token, wallet, amount_native, min_out,
            nonce=nonce, gas_limit=gas_limit, gas_fees=gas_fees,
        )

    async def build_sell_tx(self, token, wallet, amount_tokens, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict:
        return await build_sell_tx(
            self.client, self.router, token, wallet, amount_tokens, min_out,
            nonce=nonce, gas_limit=gas_limit, gas_fees=gas_fees,
        )

    async def lp_burned(self, pool: PoolRef) -> Decimal | None:
        return await lp_burned_pct(self.client, pool.address)


class V3Adapter(DexAdapter):
    """Uniswap V3: пулы с концентрированной ликвидностью."""

    kind = "v3"
    needs_unwrap = True          # продажа отдаёт WETH, его надо развернуть
    VARIANTS = ("router02", "router01")

    def __init__(self, client: ChainClient, cfg: RouterConfig) -> None:
        super().__init__(client, cfg)
        self.factory = to_checksum(cfg.factory)
        self.quoter = to_checksum(cfg.quoter) if cfg.quoter else ""
        self.fee_tiers = tuple(cfg.fee_tiers or (100, 500, 3000, 10000))
        if cfg.variant in self.VARIANTS:
            self.variant = cfg.variant
            self._locked = True
        else:
            self.variant = _variant_cache.get(self.router.lower(), "router02")
            self._locked = False

    # ------------------------------------------------------------- кодировки
    @property
    def _router_abi(self) -> list:
        return V3_ROUTER02_ABI if self.variant == "router02" else V3_ROUTER01_ABI

    def try_next_variant(self) -> bool:
        if self._locked:
            return False
        index = self.VARIANTS.index(self.variant)
        if index + 1 >= len(self.VARIANTS):
            return False
        self.variant = self.VARIANTS[index + 1]
        log.info("Роутер %s: пробую кодировку %s", self.router, self.variant)
        return True

    def remember_variant(self) -> None:
        _variant_cache[self.router.lower()] = self.variant

    def _swap_params(self, token_in: str, token_out: str, fee: int, recipient: str,
                     amount_in: int, min_out: int) -> tuple:
        base = (to_checksum(token_in), to_checksum(token_out), int(fee), to_checksum(recipient))
        if self.variant == "router01":
            return (*base, deadline(), int(amount_in), int(min_out), 0)
        return (*base, int(amount_in), int(min_out), 0)

    def _encode(self, token_in: str, token_out: str, fee: int, recipient: str,
                amount_in: int, min_out: int) -> str:
        contract = self.client.contract(self.router, self._router_abi)
        params = self._swap_params(token_in, token_out, fee, recipient, amount_in, min_out)
        return contract.encode_abi("exactInputSingle", args=[params])

    def encode_buy(self, token: str, amount_native: int, min_out: int, recipient: str, pool: PoolRef) -> str:
        return self._encode(self.wnative, token, pool.fee, recipient, amount_native, min_out)

    def encode_sell(self, token: str, amount_tokens: int, min_out: int, recipient: str, pool: PoolRef) -> str:
        return self._encode(token, self.wnative, pool.fee, recipient, amount_tokens, min_out)

    # ----------------------------------------------------------------- пулы
    async def find_pool(self, token: str) -> PoolRef | None:
        """Самый глубокий пул токена среди тиров комиссии."""
        token = to_checksum(token)
        best: PoolRef | None = None
        best_liquidity = -1
        for fee in self.fee_tiers:
            try:
                address = await self.client.call(
                    self.factory, V3_FACTORY_ABI, "getPool", token, self.wnative, int(fee)
                )
            except Exception as exc:  # noqa: BLE001 - тира может не быть
                log.debug("getPool(%s, fee=%s): %s", token, fee, exc)
                continue
            if not address or address == ZERO_ADDRESS:
                continue
            try:
                balance = int(await self.client.call(self.wnative, ERC20_ABI, "balanceOf", to_checksum(address)))
            except Exception:  # noqa: BLE001
                balance = 0
            if balance > best_liquidity:
                best_liquidity, best = balance, PoolRef(address=to_checksum(address), kind="v3", fee=int(fee))
        return best

    async def pool_state(self, token: str, pool: PoolRef, decimals: int = 18) -> PoolState:
        token = to_checksum(token)
        reserve_native = int(await self.client.call(self.wnative, ERC20_ABI, "balanceOf", pool.address))
        try:
            reserve_token = int(await self.client.call(token, ERC20_ABI, "balanceOf", pool.address))
        except Exception:  # noqa: BLE001
            reserve_token = 0

        price = Decimal(0)
        try:
            slot0 = await self.client.call(pool.address, V3_POOL_ABI, "slot0")
            token0 = to_checksum(await self.client.call(pool.address, V3_POOL_ABI, "token0"))
            price = sqrt_price_to_native(int(slot0[0]), token0 == token, decimals,
                                         self.client.config.native_decimals)
        except Exception as exc:  # noqa: BLE001 - цена не критична для решения
            log.debug("slot0(%s): %s", pool.address, exc)

        return PoolState(
            pool=pool,
            liquidity_native=from_wei(reserve_native, self.client.config.native_decimals),
            price_native=price,
            reserve_native=reserve_native,
            reserve_token=reserve_token,
            token_decimals=decimals,
        )

    # ------------------------------------------------------------ котировки
    async def _quote(self, token_in: str, token_out: str, amount_in: int, fee: int) -> int:
        if not self.quoter:
            raise DexError("Для V3 не задан адрес Quoter — котировка невозможна")
        order = _quoter_cache.get(self.quoter.lower(), "v2")
        attempts = ("v2", "v1") if order == "v2" else ("v1", "v2")
        last_error: Exception | None = None
        for kind in attempts:
            try:
                if kind == "v2":
                    result = await self.client.call(
                        self.quoter, QUOTER_V2_ABI, "quoteExactInputSingle",
                        (to_checksum(token_in), to_checksum(token_out), int(amount_in), int(fee), 0),
                    )
                    amount_out = int(result[0])
                else:
                    amount_out = int(await self.client.call(
                        self.quoter, QUOTER_V1_ABI, "quoteExactInputSingle",
                        to_checksum(token_in), to_checksum(token_out), int(fee), int(amount_in), 0,
                    ))
            except Exception as exc:  # noqa: BLE001 - вторая кодировка ещё впереди
                last_error = exc
                continue
            _quoter_cache[self.quoter.lower()] = kind
            return amount_out
        raise DexError(f"Quoter не ответил: {last_error}")

    async def quote_buy(self, token: str, amount_native: int, pool: PoolRef) -> int:
        return await self._quote(self.wnative, token, amount_native, pool.fee)

    async def quote_sell(self, token: str, amount_tokens: int, pool: PoolRef) -> int:
        return await self._quote(token, self.wnative, amount_tokens, pool.fee)

    # ------------------------------------------------------------ транзакции
    async def build_buy_tx(self, token, wallet, amount_native, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict:
        return {
            "from": to_checksum(wallet),
            "to": self.router,
            "value": int(amount_native),
            "data": self.encode_buy(token, amount_native, min_out, wallet, pool),
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": self.client.config.chain_id,
            **gas_fees,
        }

    async def build_sell_tx(self, token, wallet, amount_tokens, min_out, pool, *, nonce, gas_limit, gas_fees) -> dict:
        return {
            "from": to_checksum(wallet),
            "to": self.router,
            "value": 0,
            "data": self.encode_sell(token, amount_tokens, min_out, wallet, pool),
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": self.client.config.chain_id,
            **gas_fees,
        }

    async def build_unwrap_tx(self, wallet: str, amount: int, *, nonce: int, gas_fees: dict,
                              gas_limit: int = 120_000) -> dict:
        """WETH -> нативная монета после продажи."""
        from sniperbot.chain.abi import WETH_ABI

        contract = self.client.contract(self.wnative, WETH_ABI)
        return {
            "from": to_checksum(wallet),
            "to": self.wnative,
            "value": 0,
            "data": contract.encode_abi("withdraw", args=[int(amount)]),
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": self.client.config.chain_id,
            **gas_fees,
        }


def sqrt_price_to_native(sqrt_price_x96: int, token_is_token0: bool,
                         token_decimals: int, native_decimals: int = 18) -> Decimal:
    """Цена токена в нативной монете из `slot0().sqrtPriceX96`.

    sqrtPriceX96 = sqrt(token1/token0) * 2^96 в «сырых» единицах, поэтому после
    возведения в квадрат остаётся привести десятичные разряды.
    """
    if sqrt_price_x96 <= 0:
        return Decimal(0)
    ratio = (Decimal(sqrt_price_x96) / Q96) ** 2       # token1 за 1 token0, сырые единицы
    if not token_is_token0:
        if ratio == 0:
            return Decimal(0)
        ratio = 1 / ratio                              # переворачиваем: native -> token
    scale = Decimal(10) ** (token_decimals - native_decimals)
    return ratio * scale


def get_adapter(client: ChainClient, cfg: RouterConfig) -> DexAdapter:
    return V3Adapter(client, cfg) if cfg.is_v3 else V2Adapter(client, cfg)


async def find_best_venue(client: ChainClient, token: str, decimals: int = 18, route: str = "auto"):
    """Самый ликвидный пул токена среди всех DEX сети: (адаптер, пул, состояние).

    ``route`` ограничивает поиск версией протокола: auto | v2 | v3.
    """
    best = None
    best_liquidity = Decimal(-1)
    for adapter in adapters_for(client):
        if route in {"v2", "v3"} and adapter.kind != route:
            continue
        try:
            pool = await adapter.find_pool(token)
            if pool is None:
                continue
            state = await adapter.pool_state(token, pool, decimals)
        except Exception as exc:  # noqa: BLE001 - площадка может не отвечать
            log.debug("Пул %s для %s не проверен: %s", adapter.name, token, exc)
            continue
        if state.has_liquidity and state.liquidity_native > best_liquidity:
            best_liquidity, best = state.liquidity_native, (adapter, pool, state)
    return best


def adapters_for(client: ChainClient) -> list[DexAdapter]:
    """Адаптеры по всем настроенным DEX сети, начиная с основного."""
    routers = client.config.active_routers
    default = client.config.default_router
    if default is not None:
        routers = [default] + [r for r in routers if r is not default]
    return [get_adapter(client, cfg) for cfg in routers]
