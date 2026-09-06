"""Асинхронные RPC-клиенты сетей с автоматическим переключением эндпоинтов."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import aiohttp
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.contract import AsyncContract
from web3.exceptions import ContractLogicError

from sniperbot.chain.abi import ERC20_ABI, FACTORY_ABI, PAIR_ABI, ROUTER_ABI
from sniperbot.config import ChainConfig
from sniperbot.utils.evm import to_checksum

try:  # web3 >= 7
    from web3.middleware import ExtraDataToPOAMiddleware as _POA_MIDDLEWARE
except ImportError:  # pragma: no cover - web3 6.x
    from web3.middleware import geth_poa_middleware as _POA_MIDDLEWARE  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

T = TypeVar("T")

RPC_TIMEOUT = 12

# Ошибки, при которых имеет смысл сходить в другой RPC.
TRANSPORT_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, aiohttp.ClientError)

# Публичные ноды отвечают отказом по-разному: и кодом, и текстом.
# Всё перечисленное означает «эта нода сейчас не отдаёт данные», а не ошибку контракта.
RETRY_MARKERS = (
    "timeout", "timed out", "connection", "unreachable",
    "401", "403", "404", "408", "429", "500", "502", "503", "504",
    "too many requests", "rate limit", "limit exceeded", "capacity",
    "temporarily unavailable", "forbidden", "service unavailable",
)


def is_transport_error(exc: BaseException) -> bool:
    """Отличает «нода недоступна» от «контракт вернул ошибку»."""
    if isinstance(exc, ContractLogicError):
        return False
    if isinstance(exc, TRANSPORT_ERRORS):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in RETRY_MARKERS)


class ChainUnavailable(RuntimeError):
    """Ни один RPC сети не отвечает."""


class ChainClient:
    """Обёртка над несколькими RPC одной сети.

    Любой запрос выполняется через :meth:`run`: при сетевой ошибке клиент
    переключается на следующий эндпоинт, а ошибки контракта (revert)
    пробрасываются наверх без повторов.
    """

    def __init__(self, config: ChainConfig) -> None:
        if not config.rpc_urls:
            raise ChainUnavailable(f"Для сети {config.key} не задан ни один RPC URL")
        self.config = config
        self._providers: list[AsyncWeb3] = [self._make_w3(url) for url in config.rpc_urls]
        self._index = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ setup
    def _make_w3(self, url: str) -> AsyncWeb3:
        w3 = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": RPC_TIMEOUT}))
        if self.config.poa:
            w3.middleware_onion.inject(_POA_MIDDLEWARE, layer=0)
        return w3

    @property
    def w3(self) -> AsyncWeb3:
        return self._providers[self._index]

    @property
    def rpc_url(self) -> str:
        return self.config.rpc_urls[self._index]

    async def run(self, fn: Callable[[AsyncWeb3], Awaitable[T]]) -> T:
        """Выполняет запрос, перебирая RPC при транспортных ошибках."""
        last_error: Exception | None = None
        for offset in range(len(self._providers)):
            index = (self._index + offset) % len(self._providers)
            try:
                result = await fn(self._providers[index])
            except ContractLogicError:
                raise
            except Exception as exc:  # noqa: BLE001 - провайдеры кидают свои типы ошибок
                if not is_transport_error(exc):
                    raise
                last_error = exc
                log.debug("RPC %s недоступен: %s", self.config.rpc_urls[index], exc)
                continue
            else:
                if index != self._index:
                    log.info("Сеть %s: переключился на RPC %s", self.config.key, self.config.rpc_urls[index])
                    self._index = index
                return result
        raise ChainUnavailable(
            f"Все RPC сети {self.config.name} недоступны: {last_error}"
        ) from last_error

    async def close(self) -> None:
        """Закрывает HTTP-сессии провайдеров (иначе aiohttp ругается на выходе)."""
        for provider in self._providers:
            disconnect = getattr(provider.provider, "disconnect", None)
            if disconnect is None:
                continue
            try:
                await disconnect()
            except Exception as exc:  # noqa: BLE001 - на выходе это не важно
                log.debug("Не смог закрыть провайдера: %s", exc)

    async def healthcheck(self) -> bool:
        try:
            block = await self.run(lambda w3: w3.eth.get_block_number())
        except Exception as exc:  # noqa: BLE001
            log.warning("Сеть %s не отвечает: %s", self.config.key, exc)
            return False
        log.info("Сеть %s онлайн, блок %s (%s)", self.config.name, block, self.rpc_url)
        return True

    # -------------------------------------------------------------- contracts
    def erc20(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=ERC20_ABI)

    def router(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=ROUTER_ABI)

    def factory(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=FACTORY_ABI)

    def pair(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=PAIR_ABI)

    def contract(self, address: str, abi: list, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=abi)

    async def call(self, address: str, abi: list, fn_name: str, *args, **call_kwargs) -> Any:
        """Вызов view-функции произвольного контракта с failover."""

        async def _do(w3: AsyncWeb3) -> Any:
            contract = self.contract(address, abi, w3)
            return await contract.functions[fn_name](*args).call(**call_kwargs)

        return await self.run(_do)

    # ------------------------------------------------------------------ chain
    async def block_number(self) -> int:
        return await self.run(lambda w3: w3.eth.get_block_number())

    async def native_balance(self, address: str) -> int:
        checksum = to_checksum(address)
        return await self.run(lambda w3: w3.eth.get_balance(checksum))

    async def get_logs(self, params: dict) -> list:
        return await self.run(lambda w3: w3.eth.get_logs(params))

    async def transaction_count(self, address: str, block: str = "pending") -> int:
        checksum = to_checksum(address)
        return await self.run(lambda w3: w3.eth.get_transaction_count(checksum, block))

    async def send_raw(self, raw_tx: bytes) -> str:
        tx_hash = await self.run(lambda w3: w3.eth.send_raw_transaction(raw_tx))
        return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)

    async def wait_receipt(self, tx_hash: str, timeout: float = 120.0, poll: float = 1.0) -> dict:
        """Ожидание квитанции без привязки к конкретному провайдеру."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                receipt = await self.run(lambda w3: w3.eth.get_transaction_receipt(tx_hash))
                if receipt is not None:
                    return dict(receipt)
            except Exception:  # noqa: BLE001 - "not found" до попадания в блок
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Транзакция {tx_hash} не подтвердилась за {timeout:.0f} c")
            await asyncio.sleep(poll)

    async def gas_fees(self, multiplier: float = 1.0, priority_gwei: float = 1.0) -> dict:
        """Параметры газа для транзакции с учётом типа сети."""
        if self.config.eip1559:
            block = await self.run(lambda w3: w3.eth.get_block("latest"))
            base_fee = int(block.get("baseFeePerGas") or 0)
            if base_fee:
                priority = int(priority_gwei * 1e9)
                max_fee = int((base_fee * 2 + priority) * multiplier)
                return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}
        gas_price = await self.run(lambda w3: w3.eth.gas_price)
        return {"gasPrice": int(gas_price * multiplier)}

    async def estimate_gas(self, tx: dict) -> int:
        return await self.run(lambda w3: w3.eth.estimate_gas(tx))

    async def raw_call(self, tx: dict, state_override: dict | None = None, block: str = "latest") -> bytes:
        """eth_call с необязательным state override (нужен симулятору)."""

        async def _do(w3: AsyncWeb3) -> bytes:
            if state_override:
                return await w3.eth.call(tx, block, state_override)
            return await w3.eth.call(tx, block)

        return await self.run(_do)


class ChainRegistry:
    """Пул клиентов по всем активным сетям."""

    def __init__(self, chains: dict[str, ChainConfig]) -> None:
        self._configs = chains
        self._clients: dict[str, ChainClient] = {}

    def get(self, key: str) -> ChainClient:
        if key not in self._clients:
            config = self._configs.get(key)
            if config is None:
                raise KeyError(f"Сеть {key} не сконфигурирована")
            self._clients[key] = ChainClient(config)
        return self._clients[key]

    def config(self, key: str) -> ChainConfig:
        return self._configs[key]

    @property
    def keys(self) -> list[str]:
        return list(self._configs)

    @property
    def configs(self) -> dict[str, ChainConfig]:
        return dict(self._configs)

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()

    async def healthcheck_all(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for key in self._configs:
            try:
                result[key] = await self.get(key).healthcheck()
            except Exception as exc:  # noqa: BLE001
                log.warning("Сеть %s не инициализирована: %s", key, exc)
                result[key] = False
        return result
