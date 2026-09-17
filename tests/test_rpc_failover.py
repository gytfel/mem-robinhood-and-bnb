"""Переключение между RPC-эндпоинтами."""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest
from web3.exceptions import ContractLogicError

from sniperbot.chain.clients import (
    ChainClient,
    ChainUnavailable,
    classify,
    is_transport_error,
)
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


# ------------------------------------------------- за что именно считаем сбой
def test_a_number_inside_a_message_is_not_an_http_code():
    """«15000000» содержит «500», но это ошибка контракта, а не падение ноды.

    Из-за подстроки такие ошибки перебирали все узлы, копили счётчик сбоев и
    в итоге подменялись на «все RPC недоступны» — вместо настоящей причины.
    """
    exc = ValueError("gas required exceeds allowance (15000000)")
    assert classify(exc) == ""
    assert is_transport_error(exc) is False


@pytest.mark.parametrize("exc,reason", [
    (http_error(429), "rate_limit"),
    (Exception("Too Many Requests"), "rate_limit"),
    (Exception("your credits are exhausted"), "rate_limit"),
    (http_error(403), "forbidden"),
    (Exception("invalid api key"), "forbidden"),
    (TimeoutError(), "timeout"),
    (Exception("503 Service Unavailable"), "server"),
    (ConnectionError("connection reset"), "unreachable"),
    (Exception("query returned more than 10000 results"), "log_range"),
    (Exception("eth_getLogs block range too large"), "log_range"),
])
def test_failures_are_told_apart(exc, reason):
    """Разные причины лечатся по-разному, поэтому и считать их надо отдельно."""
    assert classify(exc) == reason


# --------------------------------------------------------- отдых и темп узлов
async def test_a_broken_endpoint_is_skipped_for_a_while():
    """Мёртвый узел не должен отнимать время у каждого следующего запроса."""
    client = make_client(2)
    seen: list[int] = []

    async def call(w3):
        index = client._providers.index(w3)
        seen.append(index)
        if index == 0:
            raise http_error(403)
        return "ok"

    assert await client.run(call) == "ok"
    seen.clear()
    assert await client.run(call) == "ok"
    assert seen == [1], "сломанный узел спрашивать заново незачем"


async def test_every_endpoint_resting_still_gets_asked():
    """Остаться совсем без сети хуже, чем потревожить уставший узел."""
    client = make_client(2)
    attempts = []

    async def failing(w3):
        attempts.append(1)
        raise http_error(403)

    with pytest.raises(ChainUnavailable):
        await client.run(failing)
    attempts.clear()

    async def working(w3):
        attempts.append(1)
        return 7

    assert await client.run(working) == 7
    assert attempts, "запрос обязан уйти даже когда все узлы на отдыхе"


async def test_a_rate_limited_node_is_asked_more_slowly():
    """Узел режет частоту — спрашиваем реже, а не ломимся и считаем сбои."""
    client = make_client(1)
    calls = []

    async def call(w3):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429)
        return "ok"

    assert await client.run(call) == "ok", "повтор после паузы обязан пройти"
    assert client.endpoints[0].pace > 0, "темп должен снизиться сам"
    assert client.dropped == 0, "запрос выполнен — потерянным его считать нельзя"


async def test_the_pace_returns_once_the_node_calms_down():
    client = make_client(1)
    endpoint = client.endpoints[0]
    endpoint.note_failure("rate_limit", http_error(429), 0.0)
    slowed = endpoint.pace
    assert slowed > 0

    for _ in range(400):
        endpoint.note_success()

    assert endpoint.pace == 0, "после долгой спокойной работы скорость возвращается"


async def test_requests_that_never_went_through_are_counted_apart():
    """«Сбой» и «запрос потерян» — разные вещи, и владелец должен видеть обе."""
    client = make_client(2)

    async def call(w3):
        raise http_error(403)

    with pytest.raises(ChainUnavailable):
        await client.run(call)

    assert client.failures == 2      # отказа два — по одному на узел
    assert client.dropped == 1       # а запрос потерян один


# ------------------------------------------------- слишком широкий запрос логов
class LogNode:
    """Узел, который отдаёт логи не больше чем за `limit` блоков."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spans: list[int] = []

    async def get_logs(self, params):  # noqa: ANN001
        span = params["toBlock"] - params["fromBlock"] + 1
        self.spans.append(span)
        if span > self.limit:
            raise ValueError("query returned more than 10000 results")
        return [{"block": params["fromBlock"]}, {"block": params["toBlock"]}]


def with_node(client: ChainClient, node: LogNode) -> ChainClient:
    client._providers = [SimpleNamespace(eth=node) for _ in client._providers]
    return client


async def test_a_wide_log_query_is_taken_in_halves():
    """Другие узлы ответят так же: лечится куском поменьше, а не переключением."""
    node = LogNode(limit=250)
    client = with_node(make_client(1), node)

    logs = await client.get_logs({"fromBlock": 1, "toBlock": 1000, "topics": []})

    assert len(logs) == 8, "части должны склеиться в один ответ"
    assert max(node.spans) == 1000 and min(node.spans) <= 250
    # Дробление идёт до тех пор, пока кусок не пролезет: 1000 → 500 → 250.
    assert client.log_span_limit == 250, "запоминается кусок, который узел взял"


async def test_the_scanner_asks_only_as_much_as_the_node_gives():
    """Иначе тот же отказ повторяется каждый опрос — и копится в счётчике сбоев."""
    from sniperbot.sniper.scanner import MAX_BLOCK_RANGE, PairScanner

    client = make_client(1)
    scanner = PairScanner(client, client.config.routers[0], handler=None)
    assert scanner._span() == MAX_BLOCK_RANGE

    client.log_span_limit = 200
    assert scanner._span() == 200


async def test_a_narrow_query_is_not_split():
    node = LogNode(limit=10_000)
    client = with_node(make_client(1), node)

    await client.get_logs({"fromBlock": 1, "toBlock": 100})

    assert node.spans == [100]
    assert client.log_span_limit == 0


# ---------------------------------------------------------- голова цепочки
async def test_the_head_block_is_asked_once_for_all_scanners():
    """Сканеров у сети столько, сколько площадок, а блок за это время тот же."""
    client = make_client(1)
    asked = []

    class Eth:
        async def get_block_number(self):
            asked.append(1)
            return 5_000_000

    client._providers = [SimpleNamespace(eth=Eth())]

    assert await client.block_number() == 5_000_000
    assert await client.block_number() == 5_000_000
    assert await client.block_number() == 5_000_000
    assert len(asked) == 1, "три сканера — один запрос к ноде"
