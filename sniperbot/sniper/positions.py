"""Монитор открытых позиций: take-profit, stop-loss и трейлинг-стоп."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.dex import quote_sell
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position, User
from sniperbot.notify import Notifier
from sniperbot.sniper.executor import Trader
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

MAX_QUOTE_FAILURES = 20


class PositionMonitor:
    """Периодически переоценивает позиции и закрывает их по правилам выхода."""

    def __init__(self, registry: ChainRegistry, trader: Trader, notifier: Notifier, settings: Settings) -> None:
        self.registry = registry
        self.trader = trader
        self.notifier = notifier
        self.settings = settings
        self._running = False
        self._failures: dict[int, int] = {}

    async def run(self) -> None:
        self._running = True
        log.info("Монитор позиций запущен (интервал %.1f c)", self.settings.position_poll_interval)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Монитор позиций: %s", exc)
            await asyncio.sleep(self.settings.position_poll_interval)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> None:
        async with session_scope() as session:
            positions = await repo.open_positions(session)
        for position in positions:
            try:
                await self.check_position(position)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("Позиция #%s: %s", position.id, exc)

    # --------------------------------------------------------------- логика
    async def current_price(self, position: Position) -> Decimal | None:
        """Цена выхода: сколько нативной монеты дадут за весь остаток позиции."""
        if position.amount_wei <= 0:
            return None
        client = self.registry.get(position.chain)
        router = position.router_address or (
            client.config.default_router.router if client.config.default_router else None
        )
        if not router:
            return None
        native_out = await quote_sell(client, router, position.token_address, position.amount_wei)
        tokens = from_wei(position.amount_wei, position.token_decimals)
        if tokens <= 0:
            return None
        return from_wei(native_out, client.config.native_decimals) / tokens

    async def check_position(self, position: Position) -> None:
        if position.amount_wei <= 0 or not position.entry_price:
            return
        try:
            price = await self.current_price(position)
        except Exception as exc:  # noqa: BLE001 - пул мог опустеть
            failures = self._failures.get(position.id, 0) + 1
            self._failures[position.id] = failures
            if failures == MAX_QUOTE_FAILURES:
                await self.notifier.send(
                    position.user_id,
                    f"⚠️ Не могу оценить позицию #{position.id} ({esc(position.token_symbol)}): {esc(exc)}\n"
                    "Похоже, ликвидность вытащили (rug). Проверьте вручную.",
                )
            return
        if price is None:
            return
        self._failures.pop(position.id, None)

        entry = position.entry_price
        change = (price / entry - 1) * 100 if entry > 0 else Decimal(0)
        peak = max(position.peak_price or price, price)

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is None or stored.status != "open":
                return
            stored.last_price = price
            stored.peak_price = peak

        if not position.auto_sell:
            return

        trigger, percent = self._exit_rule(position, change, price, peak)
        if trigger is None:
            return

        async with session_scope() as session:
            user = await session.get(User, position.user_id)
            cfg = await repo.get_settings(session, position.user_id, position.chain)
            fresh = await session.get(Position, position.id)
        if user is None or fresh is None or fresh.status != "open":
            return

        await self.notifier.send(
            position.user_id,
            f"{trigger.icon} <b>{trigger.title}</b> по {esc(position.token_symbol)} "
            f"({change:+.1f}%)\nПродаю {percent}% позиции…",
        )
        result = await self.trader.sell(user, fresh, cfg=cfg, percent=percent, reason=trigger.key)
        symbol = self.registry.config(position.chain).native_symbol
        if result.ok:
            await self.notifier.send(
                position.user_id,
                f"✅ Продано {percent}% {esc(position.token_symbol)}\n"
                f"Получено: {fmt_amount(from_wei(result.amount_out))} {symbol}\n"
                f"<a href='{result.explorer_url}'>Транзакция</a>",
            )
            if percent < 100:
                # После частичной фиксации отключаем повторный тейк-профит,
                # дальше позицией управляют стоп-лосс и трейлинг.
                async with session_scope() as session:
                    stored = await session.get(Position, position.id)
                    if stored is not None:
                        stored.take_profit_pct = 0
        else:
            await self.notifier.send(
                position.user_id,
                f"❌ Не удалось продать {esc(position.token_symbol)}: {esc(result.error)}",
            )

    def _exit_rule(self, position: Position, change: Decimal, price: Decimal, peak: Decimal):
        """Возвращает (правило, процент продажи) или (None, 0)."""
        if position.stop_loss_pct and change <= -Decimal(position.stop_loss_pct):
            return _Rule("stop_loss", "Стоп-лосс", "🛑"), 100
        if position.take_profit_pct and change >= Decimal(position.take_profit_pct):
            return _Rule("take_profit", "Тейк-профит", "🎉"), max(1, min(100, position.sell_percent or 100))
        if position.trailing_stop_pct and peak > 0:
            drop = (peak - price) / peak * 100
            if drop >= Decimal(position.trailing_stop_pct) and price > 0 and change > 0:
                return _Rule("trailing", "Трейлинг-стоп", "📉"), 100
        return None, 0


class _Rule:
    __slots__ = ("key", "title", "icon")

    def __init__(self, key: str, title: str, icon: str) -> None:
        self.key = key
        self.title = title
        self.icon = icon
