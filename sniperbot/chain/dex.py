"""Работа с Uniswap V2-совместимыми DEX: пары, котировки, транзакции свапов."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from web3.exceptions import ContractLogicError

from sniperbot.chain.abi import ERC20_ABI, FACTORY_ABI, PAIR_ABI, ROUTER_ABI
from sniperbot.chain.clients import ChainClient
from sniperbot.config import RouterConfig
from sniperbot.utils.evm import ZERO_ADDRESS, to_checksum
from sniperbot.utils.fmt import from_wei

log = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1
DEFAULT_DEADLINE = 120  # секунд на исполнение свапа


class DexError(RuntimeError):
    """Ошибка котировки/пары."""


@dataclass(slots=True)
class PairState:
    pair: str
    token: str
    wrapped_native: str
    reserve_token: int
    reserve_native: int
    token_decimals: int = 18

    @property
    def has_liquidity(self) -> bool:
        return self.reserve_token > 0 and self.reserve_native > 0

    @property
    def liquidity_native(self) -> Decimal:
        """Ликвидность в нативной монете (половина пула ≈ котируемая сторона)."""
        return from_wei(self.reserve_native, 18)

    @property
    def price_native(self) -> Decimal:
        """Спот-цена: сколько нативной монеты стоит 1 токен."""
        if self.reserve_token == 0:
            return Decimal(0)
        token_units = from_wei(self.reserve_token, self.token_decimals)
        if token_units == 0:
            return Decimal(0)
        return from_wei(self.reserve_native, 18) / token_units


async def get_pair_address(
    client: ChainClient, router_cfg: RouterConfig, token: str, quote: str | None = None
) -> str | None:
    quote = quote or client.config.wrapped_native
    try:
        pair = await client.call(
            router_cfg.factory, FACTORY_ABI, "getPair", to_checksum(token), to_checksum(quote)
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("getPair(%s) не удался: %s", token, exc)
        return None
    if not pair or pair == ZERO_ADDRESS:
        return None
    return to_checksum(pair)


async def read_pair(client: ChainClient, pair: str, token: str, token_decimals: int = 18) -> PairState:
    token = to_checksum(token)
    wrapped = to_checksum(client.config.wrapped_native)
    token0 = to_checksum(await client.call(pair, PAIR_ABI, "token0"))
    reserves = await client.call(pair, PAIR_ABI, "getReserves")
    reserve0, reserve1 = int(reserves[0]), int(reserves[1])
    if token0.lower() == token.lower():
        reserve_token, reserve_native = reserve0, reserve1
    else:
        reserve_token, reserve_native = reserve1, reserve0
    return PairState(
        pair=to_checksum(pair),
        token=token,
        wrapped_native=wrapped,
        reserve_token=reserve_token,
        reserve_native=reserve_native,
        token_decimals=token_decimals,
    )


async def amounts_out(client: ChainClient, router: str, amount_in: int, path: list[str]) -> list[int]:
    checksum_path = [to_checksum(p) for p in path]
    try:
        result = await client.call(router, ROUTER_ABI, "getAmountsOut", amount_in, checksum_path)
    except ContractLogicError as exc:
        raise DexError(f"Нет маршрута для свапа: {exc}") from exc
    return [int(x) for x in result]


async def quote_buy(client: ChainClient, router: str, token: str, amount_native: int) -> int:
    """Сколько токенов дадут за amount_native (без учёта налога токена)."""
    path = [client.config.wrapped_native, token]
    return (await amounts_out(client, router, amount_native, path))[-1]


async def quote_sell(client: ChainClient, router: str, token: str, amount_tokens: int) -> int:
    """Сколько нативной монеты дадут за amount_tokens (без учёта налога)."""
    path = [token, client.config.wrapped_native]
    return (await amounts_out(client, router, amount_tokens, path))[-1]


def apply_slippage(amount: int, slippage_bps: int) -> int:
    slippage_bps = max(0, min(9_900, slippage_bps))
    return max(0, amount * (10_000 - slippage_bps) // 10_000)


def deadline(seconds: int = DEFAULT_DEADLINE) -> int:
    return int(time.time()) + seconds


async def build_buy_tx(
    client: ChainClient,
    router: str,
    token: str,
    wallet: str,
    amount_native_wei: int,
    amount_out_min: int,
    *,
    nonce: int,
    gas_limit: int,
    gas_fees: dict,
) -> dict:
    """Транзакция покупки: swapExactETHForTokensSupportingFeeOnTransferTokens."""
    contract = client.router(router)
    path = [to_checksum(client.config.wrapped_native), to_checksum(token)]
    tx = await contract.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
        amount_out_min, path, to_checksum(wallet), deadline()
    ).build_transaction(
        {
            "from": to_checksum(wallet),
            "value": amount_native_wei,
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": client.config.chain_id,
            **gas_fees,
        }
    )
    return tx


async def build_sell_tx(
    client: ChainClient,
    router: str,
    token: str,
    wallet: str,
    amount_tokens: int,
    amount_out_min: int,
    *,
    nonce: int,
    gas_limit: int,
    gas_fees: dict,
) -> dict:
    """Транзакция продажи: swapExactTokensForETHSupportingFeeOnTransferTokens."""
    contract = client.router(router)
    path = [to_checksum(token), to_checksum(client.config.wrapped_native)]
    tx = await contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
        amount_tokens, amount_out_min, path, to_checksum(wallet), deadline()
    ).build_transaction(
        {
            "from": to_checksum(wallet),
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": client.config.chain_id,
            **gas_fees,
        }
    )
    return tx


async def build_approve_tx(
    client: ChainClient,
    token: str,
    spender: str,
    wallet: str,
    amount: int,
    *,
    nonce: int,
    gas_fees: dict,
    gas_limit: int = 100_000,
) -> dict:
    contract = client.erc20(token)
    return await contract.functions.approve(to_checksum(spender), amount).build_transaction(
        {
            "from": to_checksum(wallet),
            "nonce": nonce,
            "gas": gas_limit,
            "chainId": client.config.chain_id,
            **gas_fees,
        }
    )


async def lp_burned_pct(client: ChainClient, pair: str) -> Decimal | None:
    """Доля LP-токенов, отправленных в burn-адреса (грубая оценка «замка»)."""
    try:
        total = int(await client.call(pair, PAIR_ABI, "totalSupply"))
        if total <= 0:
            return None
        burned = 0
        for address in client.config.lp_burn_addresses:
            try:
                burned += int(await client.call(pair, ERC20_ABI, "balanceOf", to_checksum(address)))
            except Exception:  # noqa: BLE001
                continue
        return (Decimal(burned) / Decimal(total)) * 100
    except Exception as exc:  # noqa: BLE001
        log.debug("Не удалось посчитать сожжённый LP для %s: %s", pair, exc)
        return None
