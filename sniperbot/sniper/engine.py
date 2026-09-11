"""Оркестратор автоснайпа: новая пара -> проверки -> покупка для подписчиков."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from decimal import Decimal

from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.dex_adapter import PoolRef, get_adapter, route_allows
from sniperbot.chain.wallet import WalletService
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position
from sniperbot.notify import Notifier
from sniperbot.settings_registry import (
    format_hours,
    parse_variant,
    trading_allowed,
    variant_overlay,
)
from sniperbot.sniper.executor import Trader
from sniperbot.sniper.hunter import MomentumHunter
from sniperbot.sniper.safety import analyze_token, evaluate_verdict
from sniperbot.sniper.scanner import PairEvent, PairScanner
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, short_addr, to_wei

log = logging.getLogger(__name__)

MAX_PARALLEL_PAIRS = 4
EARLY_BLOCKS = 3          # сколько блоков после листинга считать «первыми»
MAX_SIM_AMOUNT = Decimal("0.05")  # верхняя граница суммы для симуляции налогов


class SniperEngine:
    """Запускает сканеры по всем активным сетям и обрабатывает найденные пары."""

    def __init__(
        self,
        registry: ChainRegistry,
        trader: Trader,
        wallets: WalletService,
        notifier: Notifier,
        settings: Settings,
    ) -> None:
        self.registry = registry
        self.trader = trader
        self.wallets = wallets
        self.notifier = notifier
        self.settings = settings
        self._tasks: list[asyncio.Task] = []
        self._scanners: list[PairScanner] = []
        self._watching = False
        self._semaphore = asyncio.Semaphore(MAX_PARALLEL_PAIRS)
        self._inflight: set[asyncio.Task] = set()
        self.hunter = MomentumHunter(self)

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        for key, config in self.registry.configs.items():
            if not config.enabled or not config.configured:
                continue
            client = self.registry.get(key)
            for router_cfg in config.routers:
                if not router_cfg.configured:
                    continue
                scanner = PairScanner(
                    client, router_cfg, self._on_pair, self.settings.scanner_poll_interval
                )
                self._scanners.append(scanner)
                self._tasks.append(asyncio.create_task(scanner.run(), name=f"scanner-{key}-{router_cfg.name}"))
        self._tasks.append(asyncio.create_task(self.watch_pending(), name="liquidity-watcher"))
        self._tasks.append(asyncio.create_task(self.hunter.run(), name="momentum-hunter"))
        log.info("Запущено сканеров: %s (+ ожидание ликвидности и перехват разгона)",
                 len(self._tasks) - 2)

    def status(self) -> list[dict]:
        """Состояние сканеров — для команды /health."""
        return [
            {
                "name": task.get_name(),
                "running": not task.done(),
                "error": str(task.exception()) if task.done() and not task.cancelled()
                and task.exception() else None,
            }
            for task in self._tasks
        ]

    async def watch_pending(self) -> None:
        """Возвращается к пулам без ликвидности, пока не истечёт окно наблюдения.

        Разработчики часто создают пару заранее, а ликвидность заливают через
        минуты или часы. Без этого цикла такие запуски терялись бы навсегда.
        """
        self._watching = True
        window = self.settings.scanner_liquidity_watch_minutes
        log.info("Наблюдение за пулами без ликвидности: окно %s мин", window)
        while self._watching:
            try:
                await self.watch_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Наблюдение за пулами: %s", exc)
            await asyncio.sleep(self.settings.scanner_watch_interval)

    async def watch_tick(self) -> None:
        now = dt.datetime.now(dt.UTC)
        window = dt.timedelta(minutes=self.settings.scanner_liquidity_watch_minutes)
        for chain_key, config in self.registry.configs.items():
            if not (config.enabled and config.configured):
                continue
            async with session_scope() as session:
                expired = await repo.expire_waiting_pairs(session, chain_key, now - window)
                pending = await repo.waiting_pairs(session, chain_key, now - window)
            if expired:
                log.info("Сеть %s: снято с ожидания пулов — %s", chain_key, expired)
            for row in pending:
                await self._recheck(chain_key, row)

    async def _recheck(self, chain_key: str, row) -> None:  # noqa: ANN001 - SeenPair
        """Проверяет, появилась ли ликвидность, и если да — запускает разбор."""
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
        except Exception as exc:  # noqa: BLE001 - пул мог быть удалён
            log.debug("Пул %s ещё не готов: %s", row.pair_address, exc)
            return
        if not state.has_liquidity:
            return

        async with session_scope() as session:
            await repo.mark_pair(session, row.id, "checking", "ликвидность появилась")

        log.info("Сеть %s: в пул %s залили ликвидность — проверяю токен", chain_key, row.pair_address)
        event = PairEvent(
            chain=chain_key, pair=row.pair_address, token=row.token_address,
            quote=client.config.wrapped_native, block=row.block_number,
            router=router_cfg, kind=row.dex_kind or "v2", fee=row.pool_fee or 0,
            pair_id=row.id,
        )
        await self._process_pair(event)

    async def stop(self) -> None:
        self._watching = False
        self.hunter.stop()
        for scanner in self._scanners:
            scanner.stop()
        for task in [*self._tasks, *self._inflight]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._inflight, return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()

    # ------------------------------------------------------------- обработка
    async def _on_pair(self, event: PairEvent) -> None:
        task = asyncio.create_task(self._process_pair(event))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _process_pair(self, event: PairEvent) -> None:
        async with self._semaphore:
            try:
                await self._process_pair_inner(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка обработки пары %s: %s", event.pair, exc)

    async def _process_pair_inner(self, event: PairEvent) -> None:
        client = self.registry.get(event.chain)
        chain = client.config

        async with session_scope() as session:
            subscribers = await repo.users_with_autosnipe(session, event.chain)
        pair_id = event.pair_id

        if not subscribers:
            return

        # Маршрут ограничивает вход: при /route v3 пара V2 не наша, и тратить на
        # неё ожидание ликвидности с симуляцией тем более незачем.
        subscribers = [(user, cfg) for user, cfg in subscribers
                       if route_allows(getattr(cfg, "dex_route", "auto"), event.kind)]
        if not subscribers:
            await self._mark(pair_id, "rejected",
                             f"пул {event.kind.upper()} не подходит под маршрут (/route)",
                             codes=["route"])
            return

        adapter = get_adapter(client, event.router)
        pool = PoolRef(address=event.pair, kind=event.kind, fee=event.fee)

        if not await self._wait_for_liquidity(adapter, event, pool):
            # Ликвидность часто заливают позже создания пары — берём пул на карандаш
            # и возвращаемся к нему, пока не истечёт окно наблюдения.
            await self._mark(pair_id, "waiting", "ждём заливку ликвидности")
            return

        # Симулируем на самой крупной сумме подписчиков: так налоги и
        # проскальзывание оцениваются по худшему сценарию.
        sim_amount = min(max((cfg.buy_amount for _, cfg in subscribers), default=Decimal("0.01")), MAX_SIM_AMOUNT)
        # Пыльный пул не имеет смысла проверять симуляцией: она дорогая по времени.
        floor = min((Decimal(str(cfg.min_liquidity or 0)) for _, cfg in subscribers), default=Decimal(0))
        state = await adapter.pool_state(event.token, pool)
        if floor and state.liquidity_native < floor:
            await self._mark(pair_id, "rejected",
                             f"ликвидность {state.liquidity_native:.3f} < минимума {floor}")
            return

        started = time.perf_counter()
        report = await analyze_token(
            client,
            adapter,
            event.token,
            amount_native_wei=to_wei(sim_amount, chain.native_decimals),
            settings=None,
            run_simulation=True,
            pool=pool,
        )
        analysis_ms = int((time.perf_counter() - started) * 1000)

        await self._record_pair_details(client, event, report, pair_id, analysis_ms)

        sniped = 0
        reject_reason = ""
        reject_codes: list[str] = []
        for user, cfg in subscribers:
            denied = evaluate_verdict(report, cfg)
            if denied:
                reject_reason = reject_reason or "; ".join(item.text for item in denied)
                reject_codes = reject_codes or [item.code for item in denied]
                continue
            blocked = await self.check_limits(user.id, event.chain, cfg)
            if blocked:
                reject_reason = reject_reason or blocked
                continue
            async with session_scope() as session:
                if await repo.is_blacklisted(session, event.chain, event.token, user.id):
                    reject_reason = reject_reason or "токен в чёрном списке"
                    continue
                owner = (report.token.owner or "").lower()
                avoid = owner and getattr(cfg, "avoid_bad_creators", False)
                if avoid and owner in await repo.bad_owners(session, user.id, event.chain):
                    reject_reason = reject_reason or "владелец уже приводил к убытку"
                    continue
            group, effective = await self.ab_variant(user.id, event.chain, cfg)
            await self.buy_for_user(user, effective, event, report, (adapter, pool), group)
            sniped += 1

        await self._mark(pair_id, "sniped" if sniped else "rejected", reject_reason or None,
                         codes=reject_codes)

    async def _wait_for_liquidity(self, adapter, event: PairEvent, pool: PoolRef) -> bool:
        """Ждём, пока в пул зальют ликвидность (обычно это отдельная транзакция)."""
        block_time = self.registry.config(event.chain).block_time
        deadline = self.settings.scanner_liquidity_wait_blocks * max(block_time, 0.2)
        elapsed = 0.0
        step = max(block_time, 0.5)
        while elapsed < deadline:
            try:
                state = await adapter.pool_state(event.token, pool)
                if state.has_liquidity:
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("pool_state(%s): %s", event.pair, exc)
            await asyncio.sleep(step)
            elapsed += step
        return False

    async def check_limits(self, user_id: int, chain: str, cfg) -> str | None:
        """Проверяет риск-лимиты пользователя. Возвращает причину отказа или None."""
        now = dt.datetime.now(dt.UTC)
        # Часы торговли проверяем первыми: если окно закрыто, остальное считать незачем.
        window = getattr(cfg, "trade_hours", "") or ""
        if not trading_allowed(window, now):
            return f"сейчас не торговое время (окно {format_hours(window)})"

        async with session_scope() as session:
            if cfg.max_positions and await repo.count_open_positions(session, user_id, chain) >= cfg.max_positions:
                return f"достигнут лимит открытых позиций ({cfg.max_positions})"
            if cfg.max_snipes_per_hour and await repo.snipes_last_hour(session, user_id, chain) >= cfg.max_snipes_per_hour:
                return f"достигнут лимит покупок в час ({cfg.max_snipes_per_hour})"

            cooldown = int(getattr(cfg, "cooldown_seconds", 0) or 0)
            if cooldown:
                last = await repo.last_position_at(session, user_id, chain, source="auto")
                if last is not None and (now - _aware(last)).total_seconds() < cooldown:
                    return f"пауза между покупками ({cooldown} c)"

            day_limit = Decimal(str(getattr(cfg, "daily_loss_limit", 0) or 0))
            if day_limit > 0:
                since = now - dt.timedelta(days=1)
                pnl = await repo.realized_pnl_since(session, user_id, chain, since)
                if pnl < 0 and abs(pnl) >= to_wei(day_limit):
                    return f"дневной лимит убытка исчерпан ({from_wei(abs(pnl)):.4f})"

            max_losses = int(getattr(cfg, "max_consecutive_losses", 0) or 0)
            if max_losses:
                streak = await repo.consecutive_losses(
                    session, user_id, chain, _aware(cfg.risk_reset_at) if cfg.risk_reset_at else None
                )
                if streak >= max_losses:
                    return f"{streak} убыточных сделок подряд — автоснайп на паузе, включите /on"
        return None

    async def ab_variant(self, user_id: int, chain: str, cfg):
        """Если включён A/B-тест, половина сделок идёт с изменёнными настройками."""
        if not getattr(cfg, "ab_enabled", False):
            return "", cfg
        variant = parse_variant(getattr(cfg, "ab_variant", ""))
        if not variant:
            return "", cfg
        async with session_scope() as session:
            group = await repo.next_ab_group(session, user_id, chain)
        return group, (variant_overlay(cfg, variant) if group == "B" else cfg)

    async def _record_pair_details(self, client, event: PairEvent, report, pair_id,
                                   analysis_ms: int = 0) -> None:
        """Дописывает в историю пулов то, что понадобится отчётам."""
        swaps = await self._count_early_swaps(client, event)
        async with session_scope() as session:
            await repo.update_seen_pair(
                session, pair_id,
                token_symbol=(report.token.symbol or "")[:32],
                token_name=(report.token.name or "")[:64],
                token_owner=report.token.owner,
                first_block_swaps=swaps,
                analysis_ms=analysis_ms,
            )

    async def _count_early_swaps(self, client, event: PairEvent) -> int:
        """Сколько сделок прошло в пуле в первые блоки — мера конкуренции."""
        from sniperbot.chain.abi import V2_SWAP_TOPIC, V3_SWAP_TOPIC

        topic = V3_SWAP_TOPIC if event.kind == "v3" else V2_SWAP_TOPIC
        try:
            logs = await client.get_logs({
                "fromBlock": event.block,
                "toBlock": event.block + EARLY_BLOCKS,
                "address": event.pair,
                "topics": [topic],
            })
        except Exception as exc:  # noqa: BLE001 - это статистика, а не торговля
            log.debug("Не смог посчитать ранние свапы %s: %s", event.pair, exc)
            return -1
        return len(logs)

    async def buy_for_user(self, user, cfg, event: PairEvent, report, venue, group: str = "",
                           source: str = "auto", headline: str = "🎯 <b>Новый токен</b>") -> None:
        chain = self.registry.config(event.chain)
        symbol = esc(report.token.symbol)
        await self.notifier.send(
            user.id,
            f"{headline} {symbol} в сети {esc(chain.name)}\n"
            f"<code>{event.token}</code>\n"
            f"Площадка: {esc(report.venue or event.router.name)}\n"
            f"Ликвидность: {fmt_amount(report.liquidity_native, 4)} {chain.native_symbol}\n"
            f"Налоги: покупка {_tax(report.buy_tax_pct)} / продажа {_tax(report.sell_tax_pct)}\n"
            f"Покупаю на {fmt_amount(cfg.buy_amount)} {chain.native_symbol}…",
        )
        try:
            result = await self.trader.buy(
                user, event.chain, event.token, cfg.buy_amount, cfg=cfg, source=source,
                pair_address=event.pair, venue=venue,
            )
        except Exception as exc:  # noqa: BLE001 - молчание после «покупаю» хуже любой ошибки
            log.exception("Покупка %s сорвалась: %s", event.token, exc)
            await self.notifier.send(
                user.id,
                f"❌ Покупка {symbol} (<code>{short_addr(event.token)}</code>) сорвалась:\n"
                f"{esc(str(exc)[:300])}\n\n"
                "Проверьте кошелёк на сканере — если монеты списались, "
                f"подберите позицию: <code>/recover {event.token}</code>",
            )
            return
        if group and result.ok and result.position_id:
            async with session_scope() as session:
                position = await session.get(Position, result.position_id)
                if position is not None:
                    position.ab_group = group
        if result.pending:
            # Деньги ушли, подтверждения ещё нет. Молчать нельзя: пользователь
            # видит списание и пустые позиции и думает, что бот потерял монеты.
            await self.notifier.send(
                user.id,
                f"⏳ <b>Транзакция отправлена</b>, сеть ещё не подтвердила\n"
                f"{esc(result.token_symbol)} · {fmt_amount(from_wei(result.amount_in))} "
                f"{chain.native_symbol}\n"
                f"<a href='{result.explorer_url}'>Посмотреть на сканере</a>\n\n"
                "Жду в фоне и сообщу, чем кончилось. Позиция появится сама, "
                "как только транзакция подтвердится.",
            )
            return
        if result.ok:
            await self.notifier.send(
                user.id,
                f"✅ <b>Куплено</b> {esc(result.token_symbol)}\n"
                f"Потрачено: {fmt_amount(from_wei(result.amount_in))} {chain.native_symbol}\n"
                f"Получено: {fmt_amount(from_wei(result.amount_out, result.token_decimals), 4)} {esc(result.token_symbol)}\n"
                f"<a href='{result.explorer_url}'>Транзакция</a> · позиция #{result.position_id}",
            )
        else:
            await self.notifier.send(
                user.id,
                f"❌ Покупка {symbol} ({short_addr(event.token)}) не удалась:\n{esc(result.error)}",
            )

    async def _mark(self, pair_id: int | None, status: str, reason: str | None,
                    codes: list[str] | None = None) -> None:
        if pair_id is None:
            return
        async with session_scope() as session:
            await repo.mark_pair(session, pair_id, status, reason)
            if codes:
                # Коды нужны отчёту /stats: по тексту причины фильтр не опознать.
                await repo.update_seen_pair(session, pair_id, reject_codes=",".join(codes)[:200])


def _aware(value):  # noqa: ANN001 - SQLite отдаёт наивные даты
    """Приводит время из БД к UTC-aware, иначе вычитание падает."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _tax(value) -> str:
    return "—" if value is None else f"{value:.1f}%"
