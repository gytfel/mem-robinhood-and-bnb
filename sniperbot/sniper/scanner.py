"""Сканер новых пулов: слушает PairCreated (V2) или PoolCreated (V3) у фабрики."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sniperbot.chain.abi import PAIR_CREATED_TOPIC, POOL_CREATED_TOPIC
from sniperbot.chain.clients import ChainClient
from sniperbot.config import RouterConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.utils.evm import to_checksum

log = logging.getLogger(__name__)


def _hex(value) -> str:  # noqa: ANN001 - ноды отдают HexBytes либо строку
    return value.hex() if hasattr(value, "hex") else str(value)

MAX_BLOCK_RANGE = 1_000  # ограничение eth_getLogs у большинства публичных нод


@dataclass(slots=True)
class PairEvent:
    chain: str
    pair: str
    token: str
    quote: str
    block: int
    router: RouterConfig
    kind: str = "v2"
    fee: int = 0
    pair_id: int | None = None


class PairScanner:
    """Опрашивает логи фабрики и отдаёт новые пары с нативной монетой."""

    def __init__(
        self,
        client: ChainClient,
        router_cfg: RouterConfig,
        handler: Callable[[PairEvent], Awaitable[None]],
        poll_interval: float = 2.0,
    ) -> None:
        self.client = client
        self.router_cfg = router_cfg
        self.handler = handler
        self.poll_interval = poll_interval
        self._running = False

    async def run(self) -> None:
        chain_key = self.client.config.key
        self._running = True
        log.info("Сканер %s (%s) запущен", chain_key, self.router_cfg.name)

        last_block = await self._load_cursor()
        errors = 0
        while self._running:
            try:
                head = await self.client.block_number()
                if last_block == 0:
                    last_block = head  # первый запуск: не разбираем историю
                if head > last_block:
                    to_block = min(head, last_block + MAX_BLOCK_RANGE)
                    events = await self._fetch(last_block + 1, to_block)
                    for event in events:
                        await self._dispatch(event)
                    last_block = to_block
                    await self._save_cursor(last_block)
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - сканер не должен умирать
                errors += 1
                log.warning("Сканер %s: ошибка (%s), попытка %s", chain_key, exc, errors)
                await asyncio.sleep(min(60.0, self.poll_interval * 2**min(errors, 5)))
                continue
            await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------- внутренности
    async def _load_cursor(self) -> int:
        async with session_scope() as session:
            state = await repo.get_scanner_state(session, self.client.config.key, self.router_cfg.factory)
            return int(state.last_block or 0)

    async def _save_cursor(self, block: int) -> None:
        async with session_scope() as session:
            state = await repo.get_scanner_state(session, self.client.config.key, self.router_cfg.factory)
            state.last_block = block

    async def _fetch(self, from_block: int, to_block: int) -> list[PairEvent]:
        is_v3 = self.router_cfg.is_v3
        topic = POOL_CREATED_TOPIC if is_v3 else PAIR_CREATED_TOPIC
        logs = await self.client.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": to_checksum(self.router_cfg.factory),
                "topics": [topic],
            }
        )
        wnative = self.client.config.wrapped_native.lower()
        events: list[PairEvent] = []
        for entry in logs:
            try:
                topics = entry["topics"]
                token0 = "0x" + _hex(topics[1])[-40:]
                token1 = "0x" + _hex(topics[2])[-40:]
                data = entry["data"]
                data_bytes = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
                if is_v3:
                    # PoolCreated(token0, token1, fee, tickSpacing, pool):
                    # fee — третий индексированный топик, адрес пула — второе слово данных.
                    fee = int(_hex(topics[3]), 16)
                    pool = "0x" + data_bytes[32:64][-20:].hex()
                else:
                    fee = 0
                    pool = "0x" + data_bytes[12:32].hex()
            except (IndexError, KeyError, ValueError) as exc:
                log.debug("Не разобрал лог %s: %s", "PoolCreated" if is_v3 else "PairCreated", exc)
                continue

            if token0.lower() == wnative:
                token, quote = token1, token0
            elif token1.lower() == wnative:
                token, quote = token0, token1
            else:
                continue  # пул без нативной монеты нам не интересен

            events.append(
                PairEvent(
                    chain=self.client.config.key,
                    pair=to_checksum(pool),
                    token=to_checksum(token),
                    quote=to_checksum(quote),
                    block=int(entry["blockNumber"]),
                    router=self.router_cfg,
                    kind="v3" if is_v3 else "v2",
                    fee=fee,
                )
            )
        if events:
            log.info("Сеть %s (%s): найдено новых пулов — %s (блоки %s-%s)",
                     self.client.config.key, self.router_cfg.name, len(events), from_block, to_block)
        return events

    async def _dispatch(self, event: PairEvent) -> None:
        async with session_scope() as session:
            if await repo.seen_pair_exists(session, event.chain, event.pair):
                return
            record = await repo.add_seen_pair(
                session,
                chain=event.chain,
                pair_address=event.pair,
                token_address=event.token,
                router_address=event.router.router,
                dex_kind=event.kind,
                pool_fee=event.fee,
                block_number=event.block,
                status="new",
            )
            event.pair_id = record.id
        try:
            await self.handler(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("Обработчик пары %s упал: %s", event.pair, exc)
