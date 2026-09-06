"""Переключение между RPC-эндпоинтами."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from web3.exceptions import ContractLogicError

from sniperbot.chain.clients import ChainClient, ChainUnavailable, is_transport_error
from sniperbot.config import ChainConfig, RouterConfig


def make_client(rpc_count: int = 3) -> ChainClient:
    config = ChainConfig(
        key="bsc", name="Test", chain_id=56,
        rpc_urls=[f"http://rpc{i}.local" for i in range(rpc_count)],
        wrapped_native="0x" + "b" * 40,
        routers=[RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)],
    )
    return ChainClient(config)


def http_error(status: int) -> aiohttp.ClientResponseError:
    request_info = SimpleNamespace(real_url="http://rpc.local", method="POST", url="http://rpc.local")
    return aiohttp.ClientResponseError(
        request_info=request_info, history=(), status=status, message="Forbidden"  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "exc",
    [
        http_error(403),
        http_error(429),
        TimeoutError(),
        ConnectionError("connection reset"),
        OSError("network unreachable"),
        Exception("503 Service Unavailable"),
        Exception("Too Many Requests"),
    ],
)
def test_transport_errors_are_retryable(exc):
    assert is_transport_error(exc) is True


def test_contract_revert_is_not_retryable():
    assert is_transport_error(ContractLogicError("execution reverted")) is False
    assert is_transport_error(ValueError("нет такого метода")) is False


async def test_switches_to_next_rpc_on_failure():
    """Первые две ноды отвечают 403 — данные берём с третьей."""
    client = make_client(3)
    seen: list[int] = []

    async def call(w3):
        index = client._providers.index(w3)
        seen.append(index)
        if index < 2:
            raise http_error(403)
        return 123

    assert await client.run(call) == 123
    assert seen == [0, 1, 2]
    # рабочая нода запоминается и используется первой
    assert client.rpc_url == "http://rpc2.local"


async def test_raises_when_all_rpcs_are_down():
    client = make_client(2)

    async def call(w3):
        raise http_error(403)

    with pytest.raises(ChainUnavailable) as info:
        await client.run(call)
    assert "недоступны" in str(info.value)


async def test_contract_error_is_not_retried():
    client = make_client(3)
    attempts = []

    async def call(w3):
        attempts.append(1)
        raise ContractLogicError("execution reverted")

    with pytest.raises(ContractLogicError):
        await client.run(call)
    assert len(attempts) == 1  # другие ноды ответят так же — смысла повторять нет
