"""Чтение метаданных и балансов ERC-20 токенов."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sniperbot.chain.abi import ERC20_ABI, ERC20_BYTES32_ABI, ERC20_LIMITS_ABI
from sniperbot.chain.clients import ChainClient
from sniperbot.utils.evm import ZERO_ADDRESS, to_checksum

log = logging.getLogger(__name__)

_CACHE_TTL = 600.0
_cache: dict[tuple[str, str], tuple[float, TokenInfo]] = {}


@dataclass(slots=True)
class TokenInfo:
    address: str
    name: str = "?"
    symbol: str = "?"
    decimals: int = 18
    total_supply: int = 0
    owner: str | None = None

    @property
    def renounced(self) -> bool:
        return self.owner is None or self.owner.lower() in {
            ZERO_ADDRESS.lower(),
            "0x000000000000000000000000000000000000dead",
        }


def _decode_bytes32(value: bytes) -> str:
    return value.rstrip(b"\x00").decode("utf-8", errors="ignore") or "?"


async def _text_field(client: ChainClient, address: str, field: str) -> str:
    try:
        value = await client.call(address, ERC20_ABI, field)
        if isinstance(value, bytes):
            return _decode_bytes32(value)
        return str(value).strip() or "?"
    except Exception:  # noqa: BLE001 - пробуем bytes32-вариант
        try:
            value = await client.call(address, ERC20_BYTES32_ABI, field)
            return _decode_bytes32(value) if isinstance(value, bytes) else str(value)
        except Exception:  # noqa: BLE001
            return "?"


async def _owner(client: ChainClient, address: str) -> str | None:
    for field in ("owner", "getOwner"):
        try:
            value = await client.call(address, ERC20_ABI, field)
            if value:
                return to_checksum(value)
        except Exception:  # noqa: BLE001 - у токена может не быть владельца
            continue
    return None


async def fetch_token(client: ChainClient, address: str, *, use_cache: bool = True) -> TokenInfo:
    """Метаданные токена. Падать не должна: недоступные поля просто пустые."""
    address = to_checksum(address)
    key = (client.config.key, address.lower())
    now = time.monotonic()
    if use_cache and (hit := _cache.get(key)) and now - hit[0] < _CACHE_TTL:
        return hit[1]

    name, symbol, decimals, supply, owner = await asyncio.gather(
        _text_field(client, address, "name"),
        _text_field(client, address, "symbol"),
        _safe_int(client, address, "decimals", 18),
        _safe_int(client, address, "totalSupply", 0),
        _owner(client, address),
    )
    info = TokenInfo(
        address=address,
        name=name[:64],
        symbol=symbol[:32],
        decimals=int(decimals),
        total_supply=int(supply),
        owner=owner,
    )
    _cache[key] = (now, info)
    return info


async def _safe_int(client: ChainClient, address: str, field: str, default: int) -> int:
    try:
        return int(await client.call(address, ERC20_ABI, field))
    except Exception:  # noqa: BLE001
        return default


async def balance_of(client: ChainClient, token: str, holder: str) -> int:
    return int(await client.call(token, ERC20_ABI, "balanceOf", to_checksum(holder)))


async def confirmed_balance(client: ChainClient, token: str, holder: str) -> int:
    """Баланс, подтверждённый всеми доступными нодами: берём максимум.

    Нода, отставшая от сети или с подрезанным состоянием, занижает баланс, но
    завысить его не может: показать токены, которых нет, ей неоткуда. Поэтому
    максимум по всем ответам — самый безопасный ответ на вопрос «они ещё мои?».
    """
    answers = await client.call_all(token, ERC20_ABI, "balanceOf", to_checksum(holder))
    return max((int(value) for value in answers), default=0)


async def allowance(client: ChainClient, token: str, owner: str, spender: str) -> int:
    return int(
        await client.call(token, ERC20_ABI, "allowance", to_checksum(owner), to_checksum(spender))
    )


async def trading_limits(client: ChainClient, token: str) -> dict[str, int | bool]:
    """Необязательные ограничения мем-токенов (maxTx / maxWallet / trading)."""
    result: dict[str, int | bool] = {}
    for field in ("maxTxAmount", "_maxTxAmount", "maxWalletSize", "_maxWalletSize", "maxWalletAmount"):
        try:
            value = int(await client.call(token, ERC20_LIMITS_ABI, field))
        except Exception:  # noqa: BLE001
            continue
        if value > 0:
            result[field.lstrip("_")] = value
    for field in ("tradingOpen", "tradingEnabled"):
        try:
            result[field] = bool(await client.call(token, ERC20_LIMITS_ABI, field))
        except Exception:  # noqa: BLE001
            continue
    return result


def clear_cache() -> None:
    _cache.clear()
