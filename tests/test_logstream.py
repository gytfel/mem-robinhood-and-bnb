"""Подписка на события: узел присылает лог сам, вместо опроса раз в две секунды.

Как и лента секвенсора, подписка — только будильник: по событию бот идёт
читать обычным путём. Поэтому проверяется не «что пришло», а «разбудили ли
вовремя и не разбудили ли зря».
"""

from __future__ import annotations

import json

import pytest

from sniperbot.chain.logstream import (
    LogStream,
    notified_block,
    subscribe_request,
    subscription_error,
    subscription_id,
)

FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"


def notification(block: str | int | None = "0x4d2") -> str:
    result = {"address": FACTORY, "topics": [TOPIC]}
    if block is not None:
        result["blockNumber"] = block
    return json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": "0xcafe", "result": result}})


# ------------------------------------------------------------------ запрос
def test_the_request_asks_only_for_what_matters():
    """Подписка на всю сеть завалила бы бот трафиком ради того же пробуждения."""
    request = json.loads(subscribe_request([FACTORY], [TOPIC]))

    assert request["method"] == "eth_subscribe"
    kind, filters = request["params"]
    assert kind == "logs"
    assert filters["address"] == [FACTORY.lower()]
    assert filters["topics"] == [[TOPIC.lower()]], "темы идут списком вариантов"


def test_an_empty_filter_means_the_whole_chain():
    filters = json.loads(subscribe_request([], []))["params"][1]
    assert filters == {}


# -------------------------------------------------------------- ответ узла
def test_the_subscription_id_is_read():
    assert subscription_id('{"jsonrpc":"2.0","id":1,"result":"0xcafe"}') == "0xcafe"


@pytest.mark.parametrize("payload", [
    '{"jsonrpc":"2.0","id":9,"result":"0xcafe"}',      # ответ на чужой запрос
    '{"jsonrpc":"2.0","id":1,"result":null}',
    '{"jsonrpc":"2.0","id":1}',
    "не json",
    "",
])
def test_a_missing_id_is_not_a_subscription(payload):
    assert subscription_id(payload) == ""


def test_the_refusal_is_quoted_as_is():
    """Узел без подписок отвечает текстом — его и надо показать человеку."""
    payload = '{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"method not supported"}}'
    assert subscription_error(payload) == "method not supported"
    assert subscription_error('{"jsonrpc":"2.0","id":1,"result":"0x1"}') == ""


# ------------------------------------------------------------- уведомления
@pytest.mark.parametrize("block,expected", [("0x4d2", 1234), (1234, 1234), ("1234", 1234)])
def test_the_block_number_is_read_in_any_form(block, expected):
    assert notified_block(notification(block)) == expected


def test_a_notification_without_a_block_still_wakes():
    """Номер нужен для отчёта, а повод посмотреть — само событие."""
    assert notified_block(notification(None)) == 0


@pytest.mark.parametrize("payload", [
    '{"jsonrpc":"2.0","id":1,"result":"0xcafe"}',      # это подтверждение подписки
    '{"method":"eth_blockNumber"}',
    "мусор",
    "",
])
def test_anything_else_is_not_a_notification(payload):
    assert notified_block(payload) is None


# ------------------------------------------------------------- пробуждение
def stream_for(addresses: set[str]) -> tuple[LogStream, list]:
    woken: list[int] = []
    return LogStream("wss://rpc.example", addresses, [TOPIC], woken.append, name="тест"), woken


async def test_an_event_wakes_the_bot():
    stream, woken = stream_for({FACTORY})
    assert await stream.handle(notification()) is True
    assert woken == [1234]
    assert stream.events == 1 and stream.last_block == 1234


async def test_the_subscription_confirmation_is_not_an_event():
    stream, woken = stream_for({FACTORY})
    assert await stream.handle('{"jsonrpc":"2.0","id":1,"result":"0xcafe"}') is False
    assert woken == []


async def test_a_broken_handler_does_not_kill_the_subscription():
    def explode(_block):
        raise RuntimeError("обработчик упал")

    stream = LogStream("wss://rpc.example", {FACTORY}, [TOPIC], explode)
    assert await stream.handle(notification()) is True


async def test_without_addresses_there_is_nothing_to_subscribe_to():
    stream, woken = stream_for(set())
    await stream.run()          # возвращается сразу, соединение не открывает
    assert stream.status() == "выключена"
    assert woken == []


def test_the_status_says_what_is_happening():
    import time

    stream, _ = stream_for({FACTORY})
    assert "нет связи" in stream.status()
    stream.connected = True
    assert "событий ещё не было" in stream.status(), "тишина и поломка — разные вещи"

    stream.events, stream.last_block = 3, 777
    stream.last_event_at = time.monotonic()
    assert "на связи" in stream.status() and "событий 3" in stream.status()
    assert "только что" in stream.status()


def test_the_status_says_how_long_the_silence_lasts():
    """Номер блока стоит на месте, пока пар нет, и читается как отставание —
    хотя означает всего лишь «пока тихо»."""
    import time

    stream, _ = stream_for({FACTORY})
    stream.connected = True
    stream.events = 5
    stream.last_event_at = time.monotonic() - 12 * 60
    assert "12 мин назад" in stream.status()


def test_the_subscription_starts_only_when_the_chain_gives_a_websocket(monkeypatch):
    """Нет адреса — нет подписки: бот продолжает работать опросом."""
    from sniperbot.config import ChainConfig, RouterConfig
    from sniperbot.sniper import engine as engine_module

    chain = ChainConfig(key="rh", name="RH", chain_id=4663, rpc_urls=["http://localhost"],
                        wrapped_native="0x" + "b" * 40,
                        routers=[RouterConfig("DEX", "0x" + "1" * 40, FACTORY, 25, True)])
    instance = engine_module.SniperEngine.__new__(engine_module.SniperEngine)
    instance.streams = {}
    instance._tasks = []

    instance._start_logs("rh", chain, [])
    assert instance.streams == {}

    chain.ws_url = "wss://rpc.example"
    created: list = []

    class FakeStream:
        def __init__(self, url, addresses, topics, poke, name="", chain_id=0):  # noqa: ANN001
            created.append((url, addresses, topics, chain_id))

        async def run(self):
            return None

    monkeypatch.setattr(engine_module, "LogStream", FakeStream)
    monkeypatch.setattr(engine_module.asyncio, "create_task", lambda coro, name="": coro.close())

    instance._start_logs("rh", chain, [])

    url, addresses, topics, chain_id = created[0]
    assert url == "wss://rpc.example"
    assert addresses == {FACTORY}, "подписываемся на фабрики, а не на все адреса подряд"
    assert len(topics) == 2, "создание пары у V2 и у V3 — разные события"
    assert chain_id == 4663, "номер сети нужен, чтобы поймать адрес от другой сети"


# --------------------------------------------- адрес от другой сети
def test_the_chain_is_asked_before_subscribing():
    from sniperbot.chain.logstream import answered_chain_id, chain_id_request

    request = json.loads(chain_id_request())
    assert request["method"] == "eth_chainId"
    assert answered_chain_id('{"jsonrpc":"2.0","id":0,"result":"0x1237"}') == 4663
    assert answered_chain_id('{"jsonrpc":"2.0","id":0,"result":4663}') == 4663


@pytest.mark.parametrize("payload", [
    '{"jsonrpc":"2.0","id":1,"result":"0x1237"}',     # ответ на другой запрос
    '{"jsonrpc":"2.0","id":0,"result":null}',
    "мусор",
])
def test_an_unclear_answer_does_not_block_the_subscription(payload):
    """Узел мог не ответить — это не повод отказываться от подписки."""
    from sniperbot.chain.logstream import answered_chain_id

    assert answered_chain_id(payload) == 0


async def test_an_endpoint_of_another_chain_is_refused():
    """Ошибка в одном слове адреса — и подписка живёт, но событий не будет никогда."""
    import aiohttp

    sent: list[str] = []

    class FakeSocket:
        async def send_str(self, payload):  # noqa: ANN001
            sent.append(payload)

        async def receive(self):
            return type("Frame", (), {"type": aiohttp.WSMsgType.TEXT,
                                      "data": '{"jsonrpc":"2.0","id":0,"result":"0x38"}'})()

    stream = LogStream("wss://rpc.example", {FACTORY}, [TOPIC], lambda _: None, chain_id=4663)

    with pytest.raises(RuntimeError) as info:
        await stream._same_chain(FakeSocket())

    assert "56" in str(info.value) and "4663" in str(info.value)
    assert "другой сети" in str(info.value)


async def test_the_right_chain_passes():
    import aiohttp

    class FakeSocket:
        async def send_str(self, payload):  # noqa: ANN001
            return None

        async def receive(self):
            return type("Frame", (), {"type": aiohttp.WSMsgType.TEXT,
                                      "data": '{"jsonrpc":"2.0","id":0,"result":"0x1237"}'})()

    stream = LogStream("wss://rpc.example", {FACTORY}, [TOPIC], lambda _: None, chain_id=4663)
    await stream._same_chain(FakeSocket())      # не должно бросить
