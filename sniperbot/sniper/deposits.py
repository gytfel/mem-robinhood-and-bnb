"""Наблюдатель за пополнением кошельков пользователей."""

from __future__ import annotations

import asyncio
import logging

from sniperbot.chain.clients import ChainRegistry
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.notify import Notifier
from sniperbot.utils.fmt import fmt_amount, from_wei

log = logging.getLogger(__name__)

CONCURRENCY = 8


class DepositWatcher:
    """Сравнивает баланс кошелька с сохранённым и сообщает о пополнении."""

    def __init__(self, registry: ChainRegistry, notifier: Notifier, settings: Settings) -> None:
        self.registry = registry
        self.notifier = notifier
        self.settings = settings
        self._running = False
        self._semaphore = asyncio.Semaphore(CONCURRENCY)

    async def run(self) -> None:
        self._running = True
        log.info("Наблюдатель пополнений запущен (интервал %.0f c)", self.settings.deposit_poll_interval)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Наблюдатель пополнений: %s", exc)
            await asyncio.sleep(self.settings.deposit_poll_interval)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> None:
        async with session_scope() as session:
            users = await repo.all_users(session)
        chains = [key for key, cfg in self.registry.configs.items() if cfg.enabled and cfg.configured]
        tasks = [self._check(user.id, user.wallet_address, chain, user.notify_deposits)
                 for user in users for chain in chains]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check(self, user_id: int, address: str | None, chain_key: str, notify: bool) -> None:
        if not address:
            return
        async with self._semaphore:
            try:
                client = self.registry.get(chain_key)
                balance = await client.native_balance(address)
            except Exception as exc:  # noqa: BLE001
                log.debug("Баланс %s в %s недоступен: %s", address, chain_key, exc)
                return

        async with session_scope() as session:
            cfg = await repo.get_settings(session, user_id, chain_key)
            previous = int(cfg.last_native_balance or 0)
            cfg.last_native_balance = balance
            if not cfg.deposit_synced:
                # Первый замер: просто запоминаем баланс, чтобы не принять
                # уже лежащие на кошельке средства за новое пополнение.
                cfg.deposit_synced = True
                return
            delta = balance - previous
            if delta <= 0:
                return
            await repo.log_wallet_event(
                session, user_id=user_id, chain=chain_key, kind="deposit",
                amount_wei=delta, balance_after_wei=balance,
            )

        if notify:
            symbol = self.registry.config(chain_key).native_symbol
            await self.notifier.send(
                user_id,
                f"💰 <b>Пополнение</b> +{fmt_amount(from_wei(delta))} {symbol}\n"
                f"Баланс: {fmt_amount(from_wei(balance))} {symbol}\n"
                f"Сеть: {self.registry.config(chain_key).name}",
            )
