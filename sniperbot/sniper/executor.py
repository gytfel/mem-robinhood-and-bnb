"""Исполнение сделок: покупка и продажа токенов с записью позиций в БД."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.dex import (
    apply_slippage,
    build_approve_tx,
    build_buy_tx,
    build_sell_tx,
    get_pair_address,
    quote_buy,
    quote_sell,
)
from sniperbot.chain.erc20 import allowance, balance_of, fetch_token
from sniperbot.chain.wallet import WalletError, WalletService
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, Position, User, utcnow
from sniperbot.utils.evm import to_checksum
from sniperbot.utils.fmt import from_wei, to_wei

log = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1
GAS_BUFFER_BPS = 13_000  # +30% к оценке газа: токены с комиссией жрут больше


class TradeError(RuntimeError):
    """Ошибка, которую можно показать пользователю."""


@dataclass(slots=True)
class TradeResult:
    ok: bool
    kind: str
    tx_hash: str | None = None
    amount_in: int = 0
    amount_out: int = 0
    position_id: int | None = None
    token_symbol: str = "?"
    token_decimals: int = 18
    error: str | None = None
    explorer_url: str | None = None


class Trader:
    """Покупка/продажа через Uniswap V2-совместимый роутер."""

    def __init__(self, registry: ChainRegistry, wallets: WalletService, settings: Settings) -> None:
        self.registry = registry
        self.wallets = wallets
        self.settings = settings

    # --------------------------------------------------------------- покупка
    async def buy(
        self,
        user: User,
        chain_key: str,
        token_address: str,
        amount_native: Decimal,
        *,
        cfg: ChainSettings,
        source: str = "manual",
        pair_address: str | None = None,
    ) -> TradeResult:
        client = self.registry.get(chain_key)
        chain = client.config
        router_cfg = chain.default_router
        if router_cfg is None:
            return TradeResult(False, "buy", error=f"Для сети {chain.name} не настроен роутер")

        token_address = to_checksum(token_address)
        account = self.wallets.account(user)
        amount_wei = to_wei(amount_native, chain.native_decimals)
        if amount_wei <= 0:
            return TradeResult(False, "buy", error="Сумма покупки должна быть больше нуля")

        fee_wei = int(amount_wei * self.settings.service_fee_rate) if self.settings.service_fee_bps else 0
        spend_wei = amount_wei - fee_wei

        gas_fees = await client.gas_fees(float(cfg.gas_multiplier))
        gas_price = int(gas_fees.get("gasPrice") or gas_fees.get("maxFeePerGas") or 0)
        balance = await client.native_balance(account.address)
        needed = amount_wei + gas_price * cfg.gas_limit
        if balance < needed:
            return TradeResult(
                False,
                "buy",
                error=(
                    f"Недостаточно {chain.native_symbol}: нужно ~{from_wei(needed):.6f}, "
                    f"на балансе {from_wei(balance):.6f}. Пополните кошелёк."
                ),
            )

        token = await fetch_token(client, token_address)
        try:
            expected = await quote_buy(client, router_cfg.router, token_address, spend_wei)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, "buy", error=f"Не удалось получить котировку: {exc}")
        amount_out_min = apply_slippage(expected, cfg.slippage_bps)

        nonce = await self.wallets.next_nonce(client, account.address)
        tx = await build_buy_tx(
            client,
            router_cfg.router,
            token_address,
            account.address,
            spend_wei,
            amount_out_min,
            nonce=nonce,
            gas_limit=cfg.gas_limit,
            gas_fees=gas_fees,
        )
        tx["gas"] = await self._gas_limit(client, tx, cfg.gas_limit)

        balance_before = await balance_of(client, token_address, account.address)
        try:
            sent = await self.wallets.send_tx(client, account, tx)
        except WalletError as exc:
            return TradeResult(False, "buy", error=str(exc), token_symbol=token.symbol)

        log.info("BUY %s %s: tx %s", token.symbol, chain_key, sent.tx_hash)
        try:
            receipt = await client.wait_receipt(sent.tx_hash, timeout=180)
        except TimeoutError as exc:
            return TradeResult(False, "buy", tx_hash=sent.tx_hash, error=str(exc), token_symbol=token.symbol)

        if int(receipt.get("status", 0)) != 1:
            await self._log(
                user.id, chain_key, "buy", token_address, spend_wei, 0, sent.tx_hash, "failed",
                error="Транзакция отклонена сетью",
            )
            return TradeResult(
                False, "buy", tx_hash=sent.tx_hash, token_symbol=token.symbol,
                error="Транзакция не прошла (revert). Обычно это высокий налог, лимит на покупку или закрытая торговля.",
                explorer_url=chain.tx_url(sent.tx_hash),
            )

        balance_after = await balance_of(client, token_address, account.address)
        received = max(0, balance_after - balance_before)
        if received == 0:
            return TradeResult(
                False, "buy", tx_hash=sent.tx_hash, token_symbol=token.symbol,
                error="Транзакция прошла, но токены не пришли (100% налог?)",
                explorer_url=chain.tx_url(sent.tx_hash),
            )

        if not pair_address:
            pair_address = await get_pair_address(client, router_cfg, token_address)

        entry_price = (from_wei(spend_wei, chain.native_decimals) / from_wei(received, token.decimals)) if received else None

        async with session_scope() as session:
            position = await repo.position_by_token(session, user.id, chain_key, token_address)
            if position is None:
                position = Position(
                    user_id=user.id,
                    chain=chain_key,
                    token_address=token_address,
                    token_symbol=token.symbol,
                    token_decimals=token.decimals,
                    pair_address=pair_address,
                    router_address=router_cfg.router,
                    source=source,
                )
                session.add(position)
            position.amount_wei += received
            position.bought_wei += received
            position.native_spent_wei += spend_wei
            position.buy_tx = sent.tx_hash
            position.status = "open"
            # усреднение цены входа по всей позиции
            total_tokens = from_wei(position.amount_wei, token.decimals)
            position.entry_price = (
                from_wei(position.native_spent_wei, chain.native_decimals) / total_tokens
                if total_tokens > 0 else entry_price
            )
            position.last_price = position.entry_price
            position.peak_price = max(position.peak_price or Decimal(0), position.entry_price or Decimal(0))
            position.take_profit_pct = cfg.take_profit_pct
            position.stop_loss_pct = cfg.stop_loss_pct
            position.trailing_stop_pct = cfg.trailing_stop_pct
            position.auto_sell = cfg.auto_sell
            position.sell_percent = cfg.sell_percent
            await session.flush()
            position_id = position.id
            await repo.log_trade(
                session, user_id=user.id, position_id=position_id, chain=chain_key, kind="buy",
                token_address=token_address, amount_in_wei=spend_wei, amount_out_wei=received,
                tx_hash=sent.tx_hash, status="success", gas_used=int(receipt.get("gasUsed", 0)),
            )

        if fee_wei > 0 and self.settings.service_fee_wallet:
            await self._send_service_fee(client, account, fee_wei)

        return TradeResult(
            True, "buy", tx_hash=sent.tx_hash, amount_in=spend_wei, amount_out=received,
            position_id=position_id, token_symbol=token.symbol, token_decimals=token.decimals,
            explorer_url=chain.tx_url(sent.tx_hash),
        )

    # -------------------------------------------------------------- продажа
    async def sell(
        self,
        user: User,
        position: Position,
        *,
        cfg: ChainSettings,
        percent: int = 100,
        reason: str = "manual",
    ) -> TradeResult:
        client = self.registry.get(position.chain)
        chain = client.config
        router_address = position.router_address or (chain.default_router.router if chain.default_router else "")
        if not router_address:
            return TradeResult(False, "sell", error="Не настроен роутер для продажи")

        account = self.wallets.account(user)
        token_address = to_checksum(position.token_address)
        percent = max(1, min(100, percent))

        on_chain_balance = await balance_of(client, token_address, account.address)
        if on_chain_balance <= 0:
            async with session_scope() as session:
                stored = await session.get(Position, position.id)
                if stored is not None:
                    stored.status = "closed"
                    stored.amount_wei = 0
                    stored.closed_at = utcnow()
            return TradeResult(False, "sell", error="На кошельке нет этих токенов — позиция закрыта",
                               token_symbol=position.token_symbol)

        amount = on_chain_balance if percent >= 100 else on_chain_balance * percent // 100
        if amount <= 0:
            return TradeResult(False, "sell", error="Слишком маленький объём для продажи")

        gas_fees = await client.gas_fees(float(cfg.gas_multiplier))
        await self._ensure_allowance(client, account, token_address, router_address, amount, cfg, gas_fees)

        try:
            expected_native = await quote_sell(client, router_address, token_address, amount)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, "sell", error=f"Нет котировки на продажу: {exc}",
                               token_symbol=position.token_symbol)
        amount_out_min = apply_slippage(expected_native, cfg.slippage_bps)

        nonce = await self.wallets.next_nonce(client, account.address)
        tx = await build_sell_tx(
            client, router_address, token_address, account.address, amount, amount_out_min,
            nonce=nonce, gas_limit=cfg.gas_limit, gas_fees=gas_fees,
        )
        tx["gas"] = await self._gas_limit(client, tx, cfg.gas_limit)

        native_before = await client.native_balance(account.address)
        try:
            sent = await self.wallets.send_tx(client, account, tx)
        except WalletError as exc:
            return TradeResult(False, "sell", error=str(exc), token_symbol=position.token_symbol)

        log.info("SELL %s %s (%s): tx %s", position.token_symbol, position.chain, reason, sent.tx_hash)
        try:
            receipt = await client.wait_receipt(sent.tx_hash, timeout=180)
        except TimeoutError as exc:
            return TradeResult(False, "sell", tx_hash=sent.tx_hash, error=str(exc),
                               token_symbol=position.token_symbol)

        if int(receipt.get("status", 0)) != 1:
            await self._log(user.id, position.chain, "sell", token_address, amount, 0, sent.tx_hash,
                            "failed", position_id=position.id, error="revert")
            return TradeResult(
                False, "sell", tx_hash=sent.tx_hash, token_symbol=position.token_symbol,
                error="Продажа отклонена сетью (honeypot или недостаточный slippage)",
                explorer_url=chain.tx_url(sent.tx_hash),
            )

        native_after = await client.native_balance(account.address)
        gas_cost = int(receipt.get("gasUsed", 0)) * int(
            receipt.get("effectiveGasPrice") or gas_fees.get("gasPrice") or gas_fees.get("maxFeePerGas") or 0
        )
        received_native = max(0, native_after - native_before + gas_cost)
        remaining = await balance_of(client, token_address, account.address)

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is not None:
                stored.amount_wei = remaining
                stored.native_returned_wei += received_native
                stored.sell_tx = sent.tx_hash
                if remaining == 0 or percent >= 100:
                    stored.status = "closed"
                    stored.closed_at = utcnow()
                await session.flush()
            await repo.log_trade(
                session, user_id=user.id, position_id=position.id, chain=position.chain, kind="sell",
                token_address=token_address, amount_in_wei=amount, amount_out_wei=received_native,
                tx_hash=sent.tx_hash, status="success", gas_used=int(receipt.get("gasUsed", 0)),
            )

        return TradeResult(
            True, "sell", tx_hash=sent.tx_hash, amount_in=amount, amount_out=received_native,
            position_id=position.id, token_symbol=position.token_symbol,
            token_decimals=position.token_decimals, explorer_url=chain.tx_url(sent.tx_hash),
        )

    # ------------------------------------------------------------ служебное
    async def _ensure_allowance(
        self, client, account, token: str, spender: str, amount: int, cfg: ChainSettings, gas_fees: dict
    ) -> None:
        current = await allowance(client, token, account.address, spender)
        if current >= amount:
            return
        approve_amount = MAX_UINT256 if cfg.approve_max else amount
        nonce = await self.wallets.next_nonce(client, account.address)
        tx = await build_approve_tx(
            client, token, spender, account.address, approve_amount, nonce=nonce, gas_fees=gas_fees
        )
        tx["gas"] = await self._gas_limit(client, tx, 120_000)
        sent = await self.wallets.send_tx(client, account, tx)
        await client.wait_receipt(sent.tx_hash, timeout=120)
        log.info("APPROVE %s -> %s: %s", token, spender, sent.tx_hash)

    async def _gas_limit(self, client, tx: dict, fallback: int) -> int:
        try:
            estimated = await client.estimate_gas({k: v for k, v in tx.items() if k != "gas"})
        except Exception:  # noqa: BLE001 - симуляция часто ревертит на свежих парах
            return fallback
        return max(fallback // 4, min(estimated * GAS_BUFFER_BPS // 10_000, 5_000_000))

    async def _send_service_fee(self, client, account, fee_wei: int) -> None:
        try:
            await self.wallets.send_native(client, account, self.settings.service_fee_wallet, fee_wei)
        except Exception as exc:  # noqa: BLE001 - комиссия не должна ломать сделку
            log.warning("Не удалось отправить сервисную комиссию: %s", exc)

    async def _log(self, user_id: int, chain: str, kind: str, token: str, amount_in: int,
                   amount_out: int, tx_hash: str, status: str, position_id: int | None = None,
                   error: str | None = None) -> None:
        async with session_scope() as session:
            await repo.log_trade(
                session, user_id=user_id, position_id=position_id, chain=chain, kind=kind,
                token_address=token, amount_in_wei=amount_in, amount_out_wei=amount_out,
                tx_hash=tx_hash, status=status, error=error,
            )
