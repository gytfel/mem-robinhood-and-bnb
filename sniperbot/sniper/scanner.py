"""Сканер новых пар: слушает событие PairCreated у фабрики DEX."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sniperbot.chain.abi import PAIR_CREATED_TOPIC
from sniperbot.chain.clients import ChainClient
from sniperbot.config import RouterConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.utils.evm import to_checksum

log = logging.getLogger(__name__)

MAX_BLOCK_RANGE = 1_000  # ограничение eth_getLogs у большинства публичных нод


@dataclass(slots=True)
class PairEvent:
    chain: str
    pair: str
    token: str
    quote: str
    block: int
    router: RouterConfig
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
        logs = await self.client.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": to_checksum(self.router_cfg.factory),
                "topics": [PAIR_CREATED_TOPIC],
            }
        )
        wnative = self.client.config.wrapped_native.lower()
        events: list[PairEvent] = []
        for entry in logs:
            try:
                token0 = "0x" + entry["topics"][1].hex()[-40:]
                token1 = "0x" + entry["topics"][2].hex()[-40:]
                data = entry["data"]
                data_bytes = bytes(data) if not isinstance(data, str) else bytes.fromhex(data[2:])
                pair = "0x" + data_bytes[12:32].hex()
            except (IndexError, KeyError, ValueError) as exc:
                log.debug("Не разобрал лог PairCreated: %s", exc)
                continue

            if token0.lower() == wnative:
                token, quote = token1, token0
            elif token1.lower() == wnative:
                token, quote = token0, token1
            else:
                continue  # пара без нативной монеты нам не интересна

            events.append(
                PairEvent(
                    chain=self.client.config.key,
                    pair=to_checksum(pair),
                    token=to_checksum(token),
                    quote=to_checksum(quote),
                    block=int(entry["blockNumber"]),
                    router=self.router_cfg,
                )
            )
        if events:
            log.info("Сеть %s: найдено новых пар — %s (блоки %s-%s)",
                     self.client.config.key, len(events), from_block, to_block)
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
