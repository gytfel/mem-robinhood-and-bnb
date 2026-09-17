"""Подписка на события через eth_subscribe: узел присылает логи сам.

Опрос `eth_getLogs` раз в две секунды на сети с блоками по 0.1 секунды — это
до двадцати блоков опоздания на каждой новой паре. Подписка убирает ожидание:
узел присылает лог, как только исполнит блок.

От потока секвенсора это отличается в двух вещах, и обе в нашу пользу здесь.
Работает через обычный RPC-адрес, а не через отдельную ленту, к которой могут
и не пустить. И приходит настоящий лог события, а не транзакция, которую ещё
надо разбирать. Проигрыш — доли секунды: узел сначала исполняет блок.

Как и лента, подписка служит будильником: по событию бот идёт читать и
проверять обычным путём. Оборвётся связь — останется опрос, как раньше.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

RECONNECT_STEPS = (1.0, 2.0, 5.0, 15.0, 30.0)
SUBSCRIBE_TIMEOUT = 15.0


def subscribe_request(addresses: list[str], topics: list[str], request_id: int = 1) -> str:
    """Запрос подписки на логи. Пустой список адресов — подписка на всю сеть."""
    filters: dict[str, object] = {}
    if addresses:
        filters["address"] = [address.lower() for address in addresses]
    if topics:
        filters["topics"] = [[topic.lower() for topic in topics]]
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "method": "eth_subscribe", "params": ["logs", filters],
    })


def subscription_id(payload: str | bytes, request_id: int = 1) -> str:
    """Идентификатор подписки из ответа узла. Пусто — узел не подписал."""
    try:
        answer = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    if not isinstance(answer, dict) or answer.get("id") != request_id:
        return ""
    result = answer.get("result")
    return result if isinstance(result, str) else ""


def subscription_error(payload: str | bytes) -> str:
    """Текст отказа, если узел не поддерживает подписку."""
    try:
        answer = json.loads(payload)
    except (TypeError, ValueError):
        return ""
    error = answer.get("error") if isinstance(answer, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return ""


def chain_id_request(request_id: int = 0) -> str:
    """Вопрос «какая ты сеть» — задаётся до подписки."""
    return json.dumps({"jsonrpc": "2.0", "id": request_id,
                       "method": "eth_chainId", "params": []})


def answered_chain_id(payload: str | bytes, request_id: int = 0) -> int:
    """Номер сети из ответа узла. 0 — узел не ответил или ответил не на это."""
    try:
        answer = json.loads(payload)
    except (TypeError, ValueError):
        return 0
    if not isinstance(answer, dict) or answer.get("id") != request_id:
        return 0
    raw = answer.get("result")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return int(raw, 16) if raw.startswith("0x") else int(raw)
    return 0


def notified_block(payload: str | bytes) -> int | None:
    """Номер блока из уведомления о логе. None — это не уведомление."""
    try:
        message = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict) or message.get("method") != "eth_subscription":
        return None
    result = (message.get("params") or {}).get("result") or {}
    raw = result.get("blockNumber")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return int(raw, 16) if raw.startswith("0x") else int(raw)
    return 0        # уведомление пришло, а номера в нём нет — будить всё равно надо


class LogStream:
    """Держит подписку на логи и будит бота на каждом событии."""

    def __init__(self, url: str, addresses: set[str], topics: list[str],
                 on_hit: Callable[[int], Awaitable[None] | None], *, name: str = "",
                 chain_id: int = 0) -> None:
        self.url = url
        self.chain_id = chain_id
        self.addresses = sorted(address for address in addresses if address)
        self.topics = topics
        self.on_hit = on_hit
        self.name = name or url
        self._running = False
        self.connected = False
        self.events = 0
        self.last_block = 0
        self.last_error = ""

    async def run(self) -> None:
        self._running = True
        if not self.addresses:
            log.info("Подписка %s: адресов нет — подписываться не на что", self.name)
            return
        log.info("Подписка на события %s: %s адресов", self.name, len(self.addresses))
        failures = 0
        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    await self._listen(session)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - обрыв связи это норма
                    self.connected = False
                    self.last_error = str(exc)[:200]
                    log.warning("Подписка %s: обрыв (%s)", self.name, self.last_error)
                    failures += 1
                if not self._running:
                    break
                await asyncio.sleep(RECONNECT_STEPS[min(failures, len(RECONNECT_STEPS) - 1)])

    def stop(self) -> None:
        self._running = False

    async def _listen(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.url, heartbeat=30,
                                      max_msg_size=32 * 1024 * 1024) as socket:
            await self._same_chain(socket)
            await socket.send_str(subscribe_request(self.addresses, self.topics))
            first = await asyncio.wait_for(socket.receive(), timeout=SUBSCRIBE_TIMEOUT)
            if first.type is not aiohttp.WSMsgType.TEXT:
                raise RuntimeError("узел ответил не текстом на запрос подписки")
            if not subscription_id(first.data):
                reason = subscription_error(first.data) or "узел не поддерживает eth_subscribe"
                raise RuntimeError(reason)

            self.connected = True
            self.last_error = ""
            log.info("Подписка %s: оформлена", self.name)
            async for frame in socket:
                if frame.type is not aiohttp.WSMsgType.TEXT:
                    continue
                await self.handle(frame.data)
                if not self._running:
                    break
        self.connected = False

    async def _same_chain(self, socket) -> None:  # noqa: ANN001 - aiohttp websocket
        """Проверяет, что адрес ведёт в ту сеть, которую мы собрались слушать.

        Адреса провайдеров различаются одним словом в имени, и подписка на
        фабрики одной сети через узел другой молча не сработает никогда:
        соединение живое, событий нет. Лучше сказать об этом сразу.
        """
        if not self.chain_id:
            return
        await socket.send_str(chain_id_request())
        frame = await asyncio.wait_for(socket.receive(), timeout=SUBSCRIBE_TIMEOUT)
        if frame.type is not aiohttp.WSMsgType.TEXT:
            return
        found = answered_chain_id(frame.data)
        if found and found != self.chain_id:
            raise RuntimeError(
                f"адрес ведёт в сеть {found}, а подписка нужна на {self.chain_id} — "
                "похоже, взят эндпоинт другой сети"
            )

    async def handle(self, payload: str | bytes) -> bool:
        """Разбирает уведомление и будит бота."""
        block = notified_block(payload)
        if block is None:
            return False
        self.events += 1
        self.last_block = max(self.last_block, block)
        try:
            result = self.on_hit(block)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - подписка важнее одного пробуждения
            log.debug("Подписка %s: обработчик не сработал (%s)", self.name, exc)
        return True

    def status(self) -> str:
        if not self.addresses:
            return "выключена"
        state = "на связи" if self.connected else f"нет связи ({self.last_error or 'подключаюсь'})"
        return f"{state} · блок {self.last_block} · событий {self.events}"
