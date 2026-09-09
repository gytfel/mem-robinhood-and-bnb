"""Фоновая задача перехвата разгона: покупка токенов, которые уже растут.

Снайп новых пар — ставка на удачу: в момент листинга о токене не известно
ничего, кроме кода контракта. Этот режим работает с уже торгующимися пулами и
ищет момент, когда сделки идут, покупок заметно больше продаж, а цена только
начала двигаться. Такой вход даёт факт вместо надежды: рынок уже показал, что
токен кому-то нужен.

Стоимость наблюдения почти нулевая: один `eth_getLogs` со списком адресов
покрывает весь список наблюдения. Тяжёлые проверки (симуляция, налоги, резервы)
запускаются только для тех пулов, которые прошли отбор по потоку сделок.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal

from sniperbot.chain.abi import V2_SWAP_TOPIC, V3_SWAP_TOPIC
from sniperbot.chain.dex_adapter import PoolRef, get_adapter
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.sniper.momentum import MomentumSignal, aggregate_swaps, evaluate_momentum, sample_age
from sniperbot.sniper.safety import analyze_token, evaluate_for_settings
from sniperbot.sniper.scanner import MAX_BLOCK_RANGE, PairEvent, PairScanner
from sniperbot.utils.evm import to_checksum
from sniperbot.utils.fmt import to_wei

log = logging.getLogger(__name__)

MAX_ADDRESSES = 100        # публичные ноды не любят длинные списки адресов в eth_getLogs
MAX_CANDIDATES = 5         # сколько пулов разбираем за один цикл: проверка дорогая
MAX_SIM_AMOUNT = Decimal("0.05")   # верхняя граница суммы для симуляции налогов
STALE_WINDOWS = 3          # замер старше этого числа окон уже не показывает разгон


class MomentumHunter:
    """Следит за списком пулов и покупает те, где начинается движение."""

    def __init__(self, engine) -> None:  # noqa: ANN001 - SniperEngine, но без циклического импорта
        self.engine = engine
        self.registry = engine.registry
        self.settings = engine.settings
        self.notifier = engine.notifier
        self._running = False
        self._cursor: dict[str, int] = {}          # сеть -> последний разобранный блок
        self._checked: dict[str, float] = {}       # пул -> когда последний раз разбирали
        self._backfilled: set[str] = set()         # сети, где история фабрик уже разобрана
        self.trending: dict[str, list[tuple[MomentumSignal, str, str]]] = {}
        self.last_tick: dict[str, dt.datetime] = {}
        self.watched: dict[str, int] = {}          # сколько пулов под наблюдением

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        self._running = True
        log.info("Перехват разгона запущен: окно %s c", self.settings.momentum_interval)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - задача не должна умирать
                log.exception("Перехват разгона: %s", exc)
            await asyncio.sleep(self.settings.momentum_interval)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> None:
        for chain_key, config in self.registry.configs.items():
            if not (config.enabled and config.configured):
                continue
            async with session_scope() as session:
                subscribers = await repo.users_with_momentum(session, chain_key)
            if not subscribers:
                continue
            await self.scan_chain(chain_key, subscribers)
        await self._prune()

    async def scan_chain(self, chain_key: str, subscribers: list) -> None:
        """Один проход по сети: собрать поток сделок, сравнить с прошлым замером."""
        client = self.registry.get(chain_key)
        now = dt.datetime.now(dt.UTC)
        max_age = max(int(getattr(cfg, "momentum_max_age_hours", 48) or 48) for _, cfg in subscribers)
        await self._backfill(chain_key, client)

        async with session_scope() as session:
            rows = await repo.momentum_watchlist(
                session, chain_key, now - dt.timedelta(hours=max_age),
                limit=self.settings.momentum_watch_limit,
            )
        self.watched[chain_key] = len(rows)
        if not rows:
            return

        wnative = client.config.wrapped_native
        pools = {
            row.pair_address.lower(): {
                "token": row.token_address, "quote": wnative,
                "kind": row.dex_kind or "v2", "row": row,
            }
            for row in rows
            if row.pair_address
        }

        logs = await self._fetch_swaps(client, chain_key, list(pools))
        stats = aggregate_swaps(logs, pools)
        self.last_tick[chain_key] = now
        if not stats:
            return

        async with session_scope() as session:
            previous = await repo.last_pool_samples(session, chain_key, list(stats))
            for address, bucket in stats.items():
                await repo.add_pool_sample(
                    session, chain=chain_key, pool_address=address,
                    token_address=pools[address]["token"], price=bucket.last_price,
                    swaps=bucket.swaps, buys=bucket.buys, sells=bucket.sells,
                    volume_wei=bucket.volume_native,
                )

        candidates = self._rank(chain_key, stats, previous, pools, subscribers)
        for signal, address, price in candidates[:MAX_CANDIDATES]:
            meta = pools[address]
            await self._consider(chain_key, meta["row"], stats[address], price, signal, subscribers)

    async def _backfill(self, chain_key: str, client) -> None:
        """Разбирает историю фабрик один раз за запуск.

        Без этого список наблюдения начинается с нуля и первые часы режим видит
        только пулы, созданные уже при работающем боте — то есть ровно те, что и
        так ловит снайп. Разбор истории даёт токены, которые торгуются давно.
        """
        if chain_key in self._backfilled:
            return
        self._backfilled.add(chain_key)
        depth = self.settings.momentum_backfill_blocks
        if depth <= 0:
            return

        head = await client.block_number()
        added = 0
        for router_cfg in client.config.routers:
            if not router_cfg.configured:
                continue
            scanner = PairScanner(client, router_cfg, _ignore, self.settings.momentum_interval)
            start = max(1, head - depth)
            while start <= head:
                stop = min(head, start + MAX_BLOCK_RANGE - 1)
                try:
                    events = await scanner._fetch(start, stop)
                except Exception as exc:  # noqa: BLE001 - история не критична
                    log.warning("Разбор истории %s (%s): %s", chain_key, router_cfg.name, exc)
                    break
                added += await self._remember(events)
                start = stop + 1
        if added:
            log.info("Сеть %s: в наблюдение добавлено пулов из истории — %s", chain_key, added)

    @staticmethod
    async def _remember(events: list[PairEvent]) -> int:
        """Кладёт найденные в истории пулы в список наблюдения."""
        added = 0
        async with session_scope() as session:
            for event in events:
                if await repo.seen_pair_exists(session, event.chain, event.pair):
                    continue
                await repo.add_seen_pair(
                    session, chain=event.chain, pair_address=event.pair,
                    token_address=event.token, router_address=event.router.router,
                    dex_kind=event.kind, pool_fee=event.fee, block_number=event.block,
                    status="watch", reason="найден при разборе истории",
                )
                added += 1
        return added

    # ------------------------------------------------------------ внутренности
    async def _fetch_swaps(self, client, chain_key: str, pools: list[str]) -> list:
        """Свапы всех наблюдаемых пулов за прошедшие блоки — одним запросом на сотню."""
        head = await client.block_number()
        cursor = self._cursor.get(chain_key)
        if cursor is None:
            # Первый проход: замер без истории, чтобы не тянуть тысячи старых логов.
            self._cursor[chain_key] = head
            return []
        from_block = max(cursor + 1, head - MAX_BLOCK_RANGE + 1)
        if from_block > head:
            return []

        logs: list = []
        for start in range(0, len(pools), MAX_ADDRESSES):
            chunk = [to_checksum(address) for address in pools[start : start + MAX_ADDRESSES]]
            try:
                logs.extend(await client.get_logs({
                    "fromBlock": from_block,
                    "toBlock": head,
                    "address": chunk,
                    "topics": [[V2_SWAP_TOPIC, V3_SWAP_TOPIC]],
                }))
            except Exception as exc:  # noqa: BLE001 - нода могла не осилить запрос
                log.warning("Перехват разгона %s: не получил логи (%s)", chain_key, exc)
                return logs
        self._cursor[chain_key] = head
        return logs

    def _rank(self, chain_key: str, stats: dict, previous: dict, pools: dict,
              subscribers: list) -> list[tuple[MomentumSignal, str, Decimal | None]]:
        """Оценивает каждый пул по самым мягким порогам среди подписчиков.

        Пороги у пользователей разные, поэтому в кандидаты пул попадает, если его
        пропустил хоть кто-то: персональные условия проверяются ещё раз перед покупкой.
        """
        window = self.settings.momentum_interval
        ranked: list[tuple[MomentumSignal, str, Decimal | None]] = []
        for address, bucket in stats.items():
            sample = previous.get(address)
            price = sample.price if sample is not None else None
            if sample is not None and sample_age(sample.created_at) > window * STALE_WINDOWS:
                price = None   # разрыв в наблюдении: рост за такой срок — это не разгон
            best: MomentumSignal | None = None
            for _, cfg in subscribers:
                signal = self._evaluate(price, bucket, cfg)
                if best is None or (signal.passed and not best.passed):
                    best = signal
                if signal.passed:
                    break
            if best is None:
                continue
            ranked.append((best, address, price))

        ranked.sort(key=lambda item: item[0].score, reverse=True)
        self.trending[chain_key] = [
            (signal, address, pools[address]["token"]) for signal, address, _ in ranked[:20]
        ]
        return [item for item in ranked if item[0].passed]

    @staticmethod
    def _evaluate(price, bucket, cfg) -> MomentumSignal:
        """Пороги разгона одного пользователя."""
        return evaluate_momentum(
            price, bucket,
            min_gain_pct=int(getattr(cfg, "momentum_min_gain_pct", 8) or 0),
            max_gain_pct=int(getattr(cfg, "momentum_max_gain_pct", 80) or 0),
            min_trades=int(getattr(cfg, "momentum_min_trades", 8) or 0),
            min_buy_ratio_pct=int(getattr(cfg, "momentum_min_buy_ratio_pct", 60) or 0),
            min_volume_wei=to_wei(Decimal(str(getattr(cfg, "momentum_min_volume", 0) or 0))),
        )

    def _cooling_down(self, address: str) -> bool:
        """Не разбираем один и тот же пул чаще, чем раз в momentum_retry_minutes."""
        last = self._checked.get(address)
        if last is None:
            return False
        return (time.monotonic() - last) < self.settings.momentum_retry_minutes * 60

    async def _consider(self, chain_key: str, row, bucket, price, signal: MomentumSignal,
                        subscribers: list) -> None:
        """Полная проверка кандидата и покупка тем, кому он подходит."""
        address = row.pair_address.lower()
        if self._cooling_down(address):
            return
        self._checked[address] = time.monotonic()

        client = self.registry.get(chain_key)
        router_cfg = client.config.router_by_address(row.router_address or "")
        if router_cfg is None:
            router_cfg = client.config.default_router
        if router_cfg is None:
            return

        adapter = get_adapter(client, router_cfg)
        pool = PoolRef(address=row.pair_address, kind=row.dex_kind or "v2", fee=row.pool_fee or 0)
        try:
            state = await adapter.pool_state(row.token_address, pool)
        except Exception as exc:  # noqa: BLE001
            log.debug("Разгон %s: пул недоступен (%s)", row.pair_address, exc)
            return
        if not state.has_liquidity:
            return

        floor = min((Decimal(str(cfg.min_liquidity or 0)) for _, cfg in subscribers), default=Decimal(0))
        if floor and state.liquidity_native < floor:
            return

        sim_amount = min(
            max((cfg.buy_amount for _, cfg in subscribers), default=Decimal("0.01")),
            MAX_SIM_AMOUNT,
        )
        report = await analyze_token(
            client, adapter, row.token_address,
            amount_native_wei=to_wei(sim_amount, client.config.native_decimals),
            settings=None, run_simulation=True, pool=pool,
        )

        event = PairEvent(
            chain=chain_key, pair=row.pair_address, token=row.token_address,
            quote=client.config.wrapped_native, block=row.block_number,
            router=router_cfg, kind=row.dex_kind or "v2", fee=row.pool_fee or 0,
            pair_id=row.id,
        )
        log.info("Разгон %s: рост %.1f%%, покупок %.0f%%, сделок %s",
                 row.token_address, signal.gain_pct, signal.buy_ratio * 100, signal.trades)

        bought = 0
        for user, cfg in subscribers:
            if not self._evaluate(price, bucket, cfg).passed:
                continue
            ok, _ = evaluate_for_settings(report, cfg)
            if not ok:
                continue   # разгон не отменяет проверок: honeypot остаётся honeypot
            if not await self._risk_ok(user, cfg, chain_key, row):
                continue
            group, effective = await self.engine.ab_variant(user.id, chain_key, cfg)
            await self.engine.buy_for_user(
                user, effective, event, report, (adapter, pool), group,
                source="momentum", headline="🚀 <b>Разгон</b>",
            )
            bought += 1
        if bought:
            async with session_scope() as session:
                await repo.mark_pair(session, row.id, "sniped", "куплен на разгоне")

    async def _risk_ok(self, user, cfg, chain_key: str, row) -> bool:
        """Чёрный список, дубль позиции и риск-лимиты пользователя."""
        async with session_scope() as session:
            if await repo.is_blacklisted(session, chain_key, row.token_address, user.id):
                return False
            existing = await repo.position_by_token(session, user.id, chain_key, row.token_address)
            if existing is not None:
                return False   # усреднение в мемкоине увеличивает убыток, а не прибыль
        return await self.engine.check_limits(user.id, chain_key, cfg) is None

    async def _prune(self) -> None:
        ttl = self.settings.momentum_sample_ttl_hours
        if ttl <= 0:
            return
        before = dt.datetime.now(dt.UTC) - dt.timedelta(hours=ttl)
        async with session_scope() as session:
            await repo.prune_pool_samples(session, before)


async def _ignore(event: PairEvent) -> None:
    """Разбор истории только пополняет список: покупать старые листинги поздно."""
