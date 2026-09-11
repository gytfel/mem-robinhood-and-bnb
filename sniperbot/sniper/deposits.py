"""Наблюдатель за пополнением кошельков пользователей."""

from __future__ import annotations

import asyncio
import logging

from sniperbot.chain.clients import ChainRegistry
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.fees import deposit_fee, referral_progress, status_for
from sniperbot.notify import Notifier
from sniperbot.utils.fmt import fmt_amount, from_wei

log = logging.getLogger(__name__)

CONCURRENCY = 8


class DepositWatcher:
    """Сравнивает баланс кошелька с сохранённым и сообщает о пополнении."""

    def __init__(self, registry: ChainRegistry, notifier: Notifier, settings: Settings,
                 wallets=None, trader=None) -> None:  # noqa: ANN001 - без циклического импорта
        self.registry = registry
        self.notifier = notifier
        self.settings = settings
        self.wallets = wallets
        self.trader = trader
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

        fee = await self._charge(user_id, chain_key, delta)

        if notify:
            symbol = self.registry.config(chain_key).native_symbol
            text = (f"💰 <b>Пополнение</b> +{fmt_amount(from_wei(delta))} {symbol}\n"
                    f"Баланс: {fmt_amount(from_wei(balance - fee))} {symbol}\n"
                    f"Сеть: {self.registry.config(chain_key).name}")
            if fee:
                policy = self.trader.fee_policy()
                async with session_scope() as session:
                    referrals = await repo.referral_count(session, user_id)
                # Комиссию сняли — значит человек не администратор и не освобождён.
                status = status_for(referrals=referrals, is_admin=False,
                                    exempt=False, policy=policy)
                text += (f"\n\nКомиссия сервиса: {fmt_amount(from_wei(fee))} {symbol} "
                         f"({status.deposit_pct:g}%)\n"
                         f"{referral_progress(status)}\nВаша ссылка: /ref")
            await self.notifier.send(user_id, text)

    async def _charge(self, user_id: int, chain_key: str, amount_wei: int) -> int:
        """Удерживает комиссию за пополнение. Возвращает удержанное в wei."""
        if self.wallets is None or self.trader is None:
            return 0
        policy = self.trader.fee_policy()
        if not policy.enabled:
            return 0

        async with session_scope() as session:
            user = await repo.get_user(session, user_id)
            referrals = await repo.referral_count(session, user_id)
        if user is None:
            return 0

        gas = await self.trader._transfer_gas_cost(chain_key)
        fee = deposit_fee(amount_wei, referrals=referrals,
                          is_admin=user_id in self.settings.admin_ids,
                          exempt=bool(user.fee_exempt), policy=policy, gas_cost_wei=gas)
        if fee <= 0:
            return 0

        try:
            client = self.registry.get(chain_key)
            account = self.wallets.account(user)
            await self.wallets.send_native(client, account, policy.wallet, fee)
        except Exception as exc:  # noqa: BLE001 - пополнение важнее комиссии
            log.warning("Комиссия за пополнение не удержана у %s: %s", user_id, exc)
            return 0

        async with session_scope() as session:
            await repo.add_fee_paid(session, user_id, fee)
            await repo.log_wallet_event(
                session, user_id=user_id, chain=chain_key, kind="fee",
                amount_wei=fee, balance_after_wei=0,
            )
        log.info("Комиссия за пополнение %s: %s wei", user_id, fee)
        return fee
