"""Исполнение сделок: покупка и продажа токенов с записью позиций в БД.

Работает и с Uniswap V2, и с V3 — различия спрятаны в :mod:`chain.dex_adapter`.
Перед отправкой каждая транзакция прогоняется через ``eth_call``: это ловит
honeypot, закрытую торговлю и слишком высокий налог до того, как потрачен газ,
а для V3 заодно определяет, какую кодировку понимает роутер.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.dex import apply_slippage
from sniperbot.chain.dex_adapter import DexAdapter, PoolRef, find_best_venue, get_adapter
from sniperbot.chain.erc20 import (
    allowance,
    balance_by_node,
    balance_of,
    confirmed_balance,
    fetch_token,
)
from sniperbot.chain.wallet import WalletError, WalletService
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, Position, User, utcnow
from sniperbot.settings_registry import effective_gas_multiplier
from sniperbot.utils.evm import to_checksum
from sniperbot.utils.fmt import from_wei, to_wei

log = logging.getLogger(__name__)

BALANCE_RECHECK_DELAY = 4.0   # пауза перед повторным опросом нод, сек
MIN_GAS_UNITS = 150_000       # ниже этого своп не стоит нигде

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
    dex: str = ""
    pending: bool = False        # транзакция отправлена, но подтверждения ещё нет


@dataclass(slots=True)
class OrphanToken:
    """Токен на кошельке, за которым не следит ни одна позиция."""

    address: str
    symbol: str
    balance: int
    decimals: int = 18


class Trader:
    """Покупка и продажа через DEX выбранной сети."""

    def __init__(self, registry: ChainRegistry, wallets: WalletService, settings: Settings) -> None:
        self.registry = registry
        self.wallets = wallets
        self.settings = settings
        # Куда сообщить о судьбе транзакции, которая подтвердилась уже после
        # ответа пользователю. Ставится приложением; без неё бот просто молчит.
        self.on_late_result: Callable[[User, TradeResult], Awaitable[None]] | None = None
        self._settling: set[asyncio.Task] = set()

    async def close(self) -> None:
        """Снимает незавершённые дожидания — вызывается при остановке бота."""
        for task in list(self._settling):
            task.cancel()
        if self._settling:
            await asyncio.gather(*self._settling, return_exceptions=True)
        self._settling.clear()

    def _settle_later(self, coro) -> None:  # noqa: ANN001 - корутина дожидания
        task = asyncio.create_task(coro)
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)

    async def gas_fees(self, client, cfg: ChainSettings, *, exit_mode: bool = False) -> dict:
        """Цена газа по режиму; на выходе применяется отдельный множитель."""
        multiplier = effective_gas_multiplier(cfg)
        if exit_mode:
            boost = Decimal(int(getattr(cfg, "exit_gas_boost_bps", 10_000) or 10_000)) / 10_000
            multiplier = max(multiplier, boost)
        return await client.gas_fees(
            float(multiplier), float(getattr(cfg, "priority_fee_gwei", 1) or 1)
        )

    # ------------------------------------------------------- выбор площадки
    async def best_venue(self, chain_key: str, token: str,
                         route: str = "auto") -> tuple[DexAdapter, PoolRef] | None:
        """Самый ликвидный пул токена. route=v2|v3 ограничивает площадку."""
        found = await find_best_venue(self.registry.get(chain_key), token, route=route)
        return (found[0], found[1]) if found else None

    def adapter_for_position(self, position: Position) -> DexAdapter:
        client = self.registry.get(position.chain)
        for cfg in client.config.active_routers:
            same_address = cfg.router.lower() == (position.router_address or "").lower()
            if same_address:
                return get_adapter(client, cfg)
        # Роутер убрали из конфига — берём любой подходящей версии.
        for cfg in client.config.active_routers:
            if cfg.kind == (position.dex_kind or "v2"):
                return get_adapter(client, cfg)
        raise TradeError("Для этой позиции не настроен DEX")

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
        venue: tuple[DexAdapter, PoolRef] | None = None,
    ) -> TradeResult:
        client = self.registry.get(chain_key)
        chain = client.config
        token_address = to_checksum(token_address)

        venue = venue or await self.best_venue(chain_key, token_address,
                                               getattr(cfg, "dex_route", "auto") or "auto")
        if venue is None:
            return TradeResult(False, "buy", error="Не нашёл пул с ликвидностью ни на одном DEX этой сети")
        adapter, pool = venue

        account = self.wallets.account(user)
        amount_wei = to_wei(amount_native, chain.native_decimals)
        if amount_wei <= 0:
            return TradeResult(False, "buy", error="Сумма покупки должна быть больше нуля")

        fee_wei = int(amount_wei * self.settings.service_fee_rate) if self.settings.service_fee_bps else 0
        spend_wei = amount_wei - fee_wei

        gas_fees = await self.gas_fees(client, cfg)
        gas_price = int(gas_fees.get("gasPrice") or gas_fees.get("maxFeePerGas") or 0)
        balance = await client.native_balance(account.address)
        paper = bool(getattr(user, "dry_run", False))
        # Ранняя проверка — по минимально возможному расходу газа. Настройка
        # gas_limit это потолок, а не цена сделки: своп тратит впятеро меньше,
        # и резервировать весь потолок значит отказывать при живых деньгах.
        if not paper and balance < amount_wei + gas_price * MIN_GAS_UNITS:
            return TradeResult(
                False, "buy",
                error=self._not_enough(chain, amount_wei, gas_price * MIN_GAS_UNITS,
                                       balance, gas_price),
            )

        token = await fetch_token(client, token_address)
        try:
            expected = await adapter.quote_buy(token_address, spend_wei, pool)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, "buy", error=f"Не удалось получить котировку: {exc}")
        if expected <= 0:
            return TradeResult(False, "buy", error="Пул не отдаёт токены за эту сумму")
        amount_out_min = apply_slippage(expected, cfg.slippage_bps)

        if getattr(user, "dry_run", False):
            return await self._paper_buy(user, chain_key, token, adapter, pool,
                                         spend_wei, expected, source, cfg)

        nonce = await self.wallets.next_nonce(client, account.address)

        async def build(nonce_value: int) -> dict:
            tx = await adapter.build_buy_tx(
                token_address, account.address, spend_wei, amount_out_min, pool,
                nonce=nonce_value, gas_limit=cfg.gas_limit, gas_fees=gas_fees,
            )
            tx["gas"] = await self._gas_limit(client, tx, cfg.gas_limit)
            return tx

        tx, error = await self._prepare(client, adapter, build, nonce)
        if tx is None:
            return TradeResult(False, "buy", token_symbol=token.symbol, dex=adapter.name,
                               error=f"Покупка не пройдёт: {error}")

        # Теперь газ известен точно: сеть требует баланс не меньше суммы плюс
        # лимит собранной транзакции, помноженный на цену газа.
        gas_cost = int(tx.get("gas", cfg.gas_limit)) * gas_price
        value = int(tx.get("value", spend_wei))
        if balance < value + gas_cost:
            return TradeResult(
                False, "buy", token_symbol=token.symbol, dex=adapter.name,
                error=self._not_enough(chain, value, gas_cost, balance, gas_price),
            )

        balance_before = await balance_of(client, token_address, account.address)
        try:
            sent = await self.wallets.send_tx(client, account, tx)
        except WalletError as exc:
            return TradeResult(False, "buy", error=str(exc), token_symbol=token.symbol)

        log.info("BUY %s %s (%s): tx %s", token.symbol, chain_key, adapter.name, sent.tx_hash)
        # Транзакция уже в сети — деньги списаны. Дальше нельзя просто вернуть
        # ошибку и забыть хэш: тогда токены придут в кошелёк, а бот о них не
        # узнает и продавать будет нечего.
        await self._log(user.id, chain_key, "buy", token_address, spend_wei, 0, sent.tx_hash,
                        "pending")
        result = await self.settle_buy(
            user, chain_key, token, adapter, pool, spend_wei, balance_before,
            sent.tx_hash, source, cfg, pair_address, fee_wei, gas_fees, timeout=180,
        )
        if result.pending:
            # Ждать в этом вызове дальше нельзя — пользователь не должен смотреть
            # в пустоту. Но и бросать транзакцию нельзя: она может подтвердиться
            # через минуту, и тогда позиция обязана появиться сама.
            self._settle_later(self._finish_later(
                user, chain_key, token, adapter, pool, spend_wei, balance_before,
                sent.tx_hash, source, cfg, pair_address, fee_wei, gas_fees,
            ))
        return result

    async def _finish_later(self, user, chain_key, token, adapter, pool, spend_wei,
                            balance_before, tx_hash, source, cfg, pair_address,
                            fee_wei, gas_fees) -> None:
        """Дожидается медленную транзакцию в фоне и сообщает, чем всё кончилось."""
        result = await self.settle_buy(
            user, chain_key, token, adapter, pool, spend_wei, balance_before,
            tx_hash, source, cfg, pair_address, fee_wei, gas_fees,
            timeout=self.settings.pending_buy_timeout,
        )
        log.info("Отложенная покупка %s: %s", tx_hash, "успех" if result.ok else result.error)
        if self.on_late_result is not None:
            await self.on_late_result(user, result)

    async def settle_buy(
        self, user: User, chain_key: str, token, adapter: DexAdapter, pool: PoolRef,
        spend_wei: int, balance_before: int, tx_hash: str, source: str, cfg: ChainSettings,
        pair_address: str | None = None, fee_wei: int = 0, gas_fees: dict | None = None,
        timeout: float = 180,
    ) -> TradeResult:
        """Доводит отправленную покупку до записанной позиции.

        Вынесено отдельно, чтобы то же самое можно было доиграть позже: при
        медленной сети ожидание квитанции истекает, но транзакция никуда не
        девается, и позиция должна появиться, когда она подтвердится.
        """
        client = self.registry.get(chain_key)
        chain = client.config
        account = self.wallets.account(user)
        explorer = chain.tx_url(tx_hash)

        try:
            receipt = await client.wait_receipt(tx_hash, timeout=timeout)
        except TimeoutError:
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                pending=True, amount_in=spend_wei, explorer_url=explorer,
                error=f"Транзакция отправлена, но сеть ещё не подтвердила её за {timeout:.0f} c",
            )
        except Exception as exc:  # noqa: BLE001 - нода могла отвалиться, а деньги уже потрачены
            log.exception("Не смог дождаться квитанции %s: %s", tx_hash, exc)
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                pending=True, amount_in=spend_wei, explorer_url=explorer,
                error=f"Не смог проверить транзакцию: {exc}",
            )

        if int(receipt.get("status", 0)) != 1:
            await self._log(user.id, chain_key, "buy", token.address, spend_wei, 0, tx_hash,
                            "failed", error="Транзакция отклонена сетью")
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                error="Транзакция не прошла (revert). Обычно это высокий налог, лимит на покупку или закрытая торговля.",
                explorer_url=explorer,
            )

        try:
            balance_after = await balance_of(client, token.address, account.address)
        except Exception as exc:  # noqa: BLE001
            log.exception("Не смог прочитать баланс токена после покупки: %s", exc)
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                pending=True, amount_in=spend_wei, explorer_url=explorer,
                error=f"Транзакция прошла, но баланс токена не прочитался: {exc}",
            )

        received = max(0, balance_after - balance_before)
        if received == 0:
            await self._log(user.id, chain_key, "buy", token.address, spend_wei, 0, tx_hash,
                            "failed", error="Токены не пришли")
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                error="Транзакция прошла, но токены не пришли (100% налог?)",
                explorer_url=explorer,
            )

        try:
            position_id = await self._store_buy(
                user, chain_key, token, adapter, pool, spend_wei, received, tx_hash,
                source, cfg, receipt, pair_address,
            )
        except Exception as exc:  # noqa: BLE001 - токены уже в кошельке, молчать нельзя
            log.exception("Токены получены, но позиция не записалась: %s", exc)
            return TradeResult(
                False, "buy", tx_hash=tx_hash, token_symbol=token.symbol, dex=adapter.name,
                amount_in=spend_wei, amount_out=received, explorer_url=explorer,
                error=(f"Токены пришли, но позиция не записалась: {exc}. "
                       f"Подберите её командой /recover {token.address}"),
            )

        if getattr(cfg, "pre_approve", False):
            # Разрешение выдаём сразу: в момент стоп-лосса лишняя транзакция
            # стоит дороже, чем сейчас.
            try:
                await self._ensure_allowance(client, adapter, account, token.address,
                                             received, cfg, gas_fees or await self.gas_fees(client, cfg))
            except Exception as exc:  # noqa: BLE001 - покупка уже состоялась
                log.warning("Предварительный approve не удался: %s", exc)

        if fee_wei > 0 and self.settings.service_fee_wallet:
            await self._send_service_fee(client, account, fee_wei)

        return TradeResult(
            True, "buy", tx_hash=tx_hash, amount_in=spend_wei, amount_out=received,
            position_id=position_id, token_symbol=token.symbol, token_decimals=token.decimals,
            explorer_url=explorer, dex=f"{adapter.name} ({pool.label})",
        )

    async def adopt(self, user: User, chain_key: str, token_address: str, *,
                    cfg: ChainSettings) -> TradeResult:
        """Заводит позицию по токенам, которые уже лежат в кошельке.

        Нужно, когда покупка прошла в сети, а записать её боту помешал сбой:
        монеты есть, а автопродажа о них не знает и не защитит.
        """
        client = self.registry.get(chain_key)
        token_address = to_checksum(token_address)
        account = self.wallets.account(user)

        async with session_scope() as session:
            existing = await repo.position_by_token(session, user.id, chain_key, token_address)
            if existing is not None:
                return TradeResult(False, "buy", position_id=existing.id,
                                   error=f"Позиция #{existing.id} по этому токену уже открыта")

        token = await fetch_token(client, token_address)
        # Спрашиваем все ноды: подбор — это как раз инструмент для случая, когда
        # одна нода соврала нулём, и полагаться здесь на один ответ бессмысленно.
        answers = await balance_by_node(client, token_address, account.address)
        balance = max(answers, default=0)
        if balance <= 0:
            detail = (f"Опросил нод: {len(answers)}, все ответили нулём."
                      if answers else "Ни одна нода не ответила — попробуйте позже.")
            return TradeResult(
                False, "buy", token_symbol=token.symbol,
                error=("На кошельке нет этого токена — подбирать нечего.\n"
                       f"{detail}\n"
                       f"Проверить самому: {client.config.address_url(account.address)}"),
            )

        venue = await self.best_venue(chain_key, token_address,
                                      getattr(cfg, "dex_route", "auto") or "auto")
        if venue is None:
            return TradeResult(False, "buy", token_symbol=token.symbol,
                               error="Пул с ликвидностью не найден — цену взять неоткуда")
        adapter, pool = venue

        state = await adapter.pool_state(token_address, pool, token.decimals)
        # price_native — цена за целый токен, а balance в сырых единицах:
        # без приведения оценка позиции разъедется на десятки порядков.
        value = state.price_native * from_wei(balance, token.decimals)
        value_wei = to_wei(value, client.config.native_decimals) if value > 0 else 0
        if value_wei <= 0:
            return TradeResult(False, "buy", token_symbol=token.symbol,
                               error="Пул не даёт цену — позицию нельзя оценить")

        # Если позиция уже была и её списали как утраченную, возвращаем именно её:
        # новая запись означала бы, что убыток по старой посчитан вторым разом.
        async with session_scope() as session:
            lost = await repo.lost_position_by_token(session, user.id, chain_key, token_address)
            if lost is not None:
                lost.status = "open"
                lost.amount_wei = balance
                lost.exit_reason = ""
                lost.closed_at = None
                lost.last_price = state.price_native
                lost.peak_price = max(lost.peak_price or Decimal(0), state.price_native)
                return TradeResult(
                    True, "buy", amount_in=lost.native_spent_wei, amount_out=balance,
                    position_id=lost.id, token_symbol=token.symbol,
                    token_decimals=token.decimals, dex=f"{adapter.name} ({pool.label})",
                )

        position_id = await self._store_buy(
            user, chain_key, token, adapter, pool, value_wei, balance, None,
            "recover", cfg, {}, pool.address,
        )
        return TradeResult(
            True, "buy", amount_in=value_wei, amount_out=balance, position_id=position_id,
            token_symbol=token.symbol, token_decimals=token.decimals,
            dex=f"{adapter.name} ({pool.label})",
        )

    async def find_orphans(self, user: User, chain_key: str) -> list[OrphanToken]:
        """Токены на кошельке, за которыми не следит ни одна открытая позиция.

        Избавляет от угадывания адреса: покупок бывает несколько подряд, адреса
        похожи, и подобрать не тот токен слишком легко.
        """
        client = self.registry.get(chain_key)
        account = self.wallets.account(user)

        async with session_scope() as session:
            candidates = await repo.recent_token_addresses(session, user.id, chain_key)
            tracked = {
                position.token_address.lower()
                for position in await repo.open_positions(session, user_id=user.id,
                                                          chain=chain_key)
            }

        found: list[OrphanToken] = []
        for address in candidates:
            if address in tracked:
                continue
            try:
                balance = max(await balance_by_node(client, address, account.address), default=0)
            except Exception as exc:  # noqa: BLE001 - один битый токен не должен всё ронять
                log.debug("Баланс %s не прочитался: %s", address, exc)
                continue
            if balance <= 0:
                continue
            try:
                token = await fetch_token(client, address)
                symbol, decimals = token.symbol, token.decimals
            except Exception:  # noqa: BLE001 - без имени токен всё равно можно подобрать
                symbol, decimals = "?", 18
            found.append(OrphanToken(to_checksum(address), symbol, balance, decimals))
        return found

    @staticmethod
    def _not_enough(chain, amount_wei: int, gas_cost: int, balance: int, gas_price: int) -> str:
        """Отказ по деньгам с разбором: сколько на сделку, сколько на газ.

        Газ — плата за транзакцию, а не процент от суммы: он одинаков и для
        0.0002, и для 1 монеты. Если он съедает вход, дело не в балансе, и
        сообщение должно говорить об этом прямо.
        """
        needed = amount_wei + gas_cost
        text = (f"Недостаточно {chain.native_symbol}: нужно ~{from_wei(needed, chain.native_decimals):.6f} "
                f"(покупка {from_wei(amount_wei, chain.native_decimals):.6f} + газ "
                f"{from_wei(gas_cost, chain.native_decimals):.6f}), "
                f"на балансе {from_wei(balance, chain.native_decimals):.6f}.")
        if amount_wei > 0 and gas_cost * 2 > amount_wei:
            # Круг стоит дороже половины входа — торговать такой суммой бессмысленно.
            sane = gas_cost * 2 * 20      # газ на круг должен быть не больше 5% входа
            text += (f"\n\n⚠️ Газ за круг «купил-продал» — около "
                     f"{from_wei(gas_cost * 2, chain.native_decimals):.6f} {chain.native_symbol}. "
                     f"При входе {from_wei(amount_wei, chain.native_decimals):.6f} это дороже самой сделки: "
                     "прибыль невозможна в принципе.\n"
                     f"Разумный минимум сейчас: <code>/set buy "
                     f"{from_wei(sane, chain.native_decimals):.4f}</code>")
        else:
            text += " Пополните кошелёк."
        return text

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
        try:
            adapter = self.adapter_for_position(position)
        except TradeError as exc:
            return TradeResult(False, "sell", error=str(exc), token_symbol=position.token_symbol)
        pool = PoolRef(address=position.pair_address or "", kind=position.dex_kind or "v2",
                       fee=position.pool_fee or 0)

        token_address = to_checksum(position.token_address)
        percent = max(1, min(100, percent))

        if position.is_paper:
            return await self._paper_sell(position, adapter, percent, reason)

        account = self.wallets.account(user)
        on_chain_balance = await balance_of(client, token_address, account.address)
        if on_chain_balance <= 0:
            # Списать позицию по одному нулю нельзя: отставшая нода отвечает нулём
            # без ошибки, и тогда бот сам вычёркивает токены, которые никуда не
            # девались. Переспрашиваем все ноды и ждём — вдруг узел просто отстал.
            on_chain_balance = await confirmed_balance(client, token_address, account.address)
            if on_chain_balance <= 0:
                await asyncio.sleep(BALANCE_RECHECK_DELAY)
                on_chain_balance = await confirmed_balance(client, token_address, account.address)

        if on_chain_balance <= 0:
            log.warning("Позиция #%s: токенов %s нет ни на одной ноде", position.id,
                        position.token_symbol)
            async with session_scope() as session:
                stored = await session.get(Position, position.id)
                if stored is not None:
                    stored.status = "closed"
                    stored.amount_wei = 0
                    stored.exit_reason = "lost"
                    stored.closed_at = utcnow()
            return TradeResult(
                False, "sell", token_symbol=position.token_symbol,
                error=("Токенов нет на кошельке — продавать нечего. Так бывает, когда "
                       "контракт забирает баланс у держателей. Позиция закрыта как утраченная.\n"
                       f"Проверьте кошелёк: {chain.address_url(account.address)}\n"
                       f"Если токены на месте — верните позицию: /recover {token_address}"),
            )

        amount = on_chain_balance if percent >= 100 else on_chain_balance * percent // 100
        if amount <= 0:
            return TradeResult(False, "sell", error="Слишком маленький объём для продажи")

        # Выходить важнее, чем экономить: газ и проскальзывание для продажи свои.
        gas_fees = await self.gas_fees(client, cfg, exit_mode=True)
        await self._ensure_allowance(client, adapter, account, token_address, amount, cfg, gas_fees)

        try:
            expected_native = await adapter.quote_sell(token_address, amount, pool)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, "sell", error=f"Нет котировки на продажу: {exc}",
                               token_symbol=position.token_symbol)

        nonce = await self.wallets.next_nonce(client, account.address)
        slippage = int(getattr(cfg, "exit_slippage_bps", 0) or cfg.slippage_bps)

        tx, error = None, ""
        for attempt_slippage in (slippage, min(9_000, slippage * 2)):
            amount_out_min = apply_slippage(expected_native, attempt_slippage)

            async def build(nonce_value: int, minimum: int = amount_out_min) -> dict:
                built = await adapter.build_sell_tx(
                    token_address, account.address, amount, minimum, pool,
                    nonce=nonce_value, gas_limit=cfg.gas_limit, gas_fees=gas_fees,
                )
                built["gas"] = await self._gas_limit(client, built, cfg.gas_limit)
                return built

            tx, error = await self._prepare(client, adapter, build, nonce)
            if tx is not None:
                break
            log.info("Продажа %s не проходит при slippage %.1f%% — пробую шире",
                     position.token_symbol, attempt_slippage / 100)
        if tx is None:
            return TradeResult(False, "sell", token_symbol=position.token_symbol,
                               error=f"Продажа не пройдёт: {error}")

        native_before = await client.native_balance(account.address)
        wrapped_before = await self._wrapped_balance(client, adapter, account.address)
        try:
            sent = await self.wallets.send_tx(client, account, tx)
        except WalletError as exc:
            return TradeResult(False, "sell", error=str(exc), token_symbol=position.token_symbol)

        log.info("SELL %s %s (%s, %s): tx %s", position.token_symbol, position.chain,
                 adapter.name, reason, sent.tx_hash)
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

        received_native = await self._settle_sell(
            client, adapter, account, receipt, gas_fees, native_before, wrapped_before
        )
        remaining = await balance_of(client, token_address, account.address)

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is not None:
                stored.amount_wei = remaining
                stored.native_returned_wei += received_native
                stored.sell_tx = sent.tx_hash
                stored.exit_reason = reason
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
            dex=adapter.name,
        )

    # -------------------------------------------------------- тестовый режим
    async def _paper_buy(self, user, chain_key, token, adapter, pool, spend_wei,
                         expected, source, cfg) -> TradeResult:
        """Покупка «на бумаге»: позиция создаётся, деньги не тратятся."""
        chain = self.registry.config(chain_key)
        async with session_scope() as session:
            position = Position(
                user_id=user.id, chain=chain_key, token_address=token.address,
                token_symbol=token.symbol, token_decimals=token.decimals,
                pair_address=pool.address, router_address=adapter.router,
                dex_kind=adapter.kind, pool_fee=pool.fee, source=source, is_paper=True,
                amount_wei=expected, bought_wei=expected, native_spent_wei=spend_wei,
                take_profit_pct=cfg.take_profit_pct, stop_loss_pct=cfg.stop_loss_pct,
                trailing_stop_pct=cfg.trailing_stop_pct, auto_sell=cfg.auto_sell,
                sell_percent=cfg.sell_percent,
            )
            entry = from_wei(spend_wei, chain.native_decimals) / from_wei(expected, token.decimals)
            position.entry_price = entry
            position.last_price = entry
            position.peak_price = entry
            session.add(position)
            await session.flush()
            position_id = position.id
        log.info("PAPER BUY %s %s: %s", token.symbol, chain_key, from_wei(spend_wei))
        return TradeResult(
            True, "buy", amount_in=spend_wei, amount_out=expected, position_id=position_id,
            token_symbol=token.symbol, token_decimals=token.decimals,
            dex=f"{adapter.name} ({pool.label})", tx_hash=None,
        )

    async def _paper_sell(self, position: Position, adapter: DexAdapter, percent: int,
                          reason: str = "manual") -> TradeResult:
        """Продажа «на бумаге» по текущей котировке пула."""
        pool = PoolRef(address=position.pair_address or "", kind=position.dex_kind or "v2",
                       fee=position.pool_fee or 0)
        amount = position.amount_wei if percent >= 100 else position.amount_wei * percent // 100
        try:
            received = await adapter.quote_sell(position.token_address, amount, pool)
        except Exception as exc:  # noqa: BLE001
            return TradeResult(False, "sell", error=f"Нет котировки на продажу: {exc}",
                               token_symbol=position.token_symbol)

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is not None:
                stored.amount_wei = max(0, stored.amount_wei - amount)
                stored.native_returned_wei += received
                stored.exit_reason = reason
                if stored.amount_wei == 0 or percent >= 100:
                    stored.status = "closed"
                    stored.closed_at = utcnow()
        log.info("PAPER SELL %s: %s", position.token_symbol, from_wei(received))
        return TradeResult(
            True, "sell", amount_in=amount, amount_out=received, position_id=position.id,
            token_symbol=position.token_symbol, token_decimals=position.token_decimals,
            dex=adapter.name, tx_hash=None,
        )

    # ------------------------------------------------------------ служебное
    async def _prepare(self, client, adapter: DexAdapter, build, nonce: int) -> tuple[dict | None, str]:
        """Собирает транзакцию и проверяет её через eth_call до отправки.

        Если роутер V3 не понял кодировку, пробуем следующую — так вариант
        SwapRouter02 / SwapRouter определяется сам, без настроек.
        """
        last_error = "неизвестная причина"
        for _ in range(3):
            tx = await build(nonce)
            ok, error = await self._simulate(client, tx)
            if ok:
                if hasattr(adapter, "remember_variant"):
                    adapter.remember_variant()
                return tx, ""
            last_error = error
            if not adapter.try_next_variant():
                break
        return None, last_error

    async def _simulate(self, client, tx: dict) -> tuple[bool, str]:
        call = {key: tx[key] for key in ("from", "to", "data") if key in tx}
        if tx.get("value"):
            call["value"] = int(tx["value"])
        try:
            await client.raw_call(call)
        except Exception as exc:  # noqa: BLE001 - причина уходит пользователю
            return False, _revert_reason(exc)
        return True, ""

    async def _wrapped_balance(self, client, adapter: DexAdapter, address: str) -> int:
        if not adapter.needs_unwrap:
            return 0
        try:
            return await balance_of(client, client.config.wrapped_native, address)
        except Exception:  # noqa: BLE001
            return 0

    async def _settle_sell(self, client, adapter: DexAdapter, account, receipt, gas_fees,
                           native_before: int, wrapped_before: int) -> int:
        """Сколько нативной монеты получено; для V3 разворачивает WETH."""
        if adapter.needs_unwrap:
            wrapped_after = await self._wrapped_balance(client, adapter, account.address)
            received = max(0, wrapped_after - wrapped_before)
            if received > 0:
                await self._unwrap(client, adapter, account, received, gas_fees)
            return received

        native_after = await client.native_balance(account.address)
        gas_cost = int(receipt.get("gasUsed", 0)) * int(
            receipt.get("effectiveGasPrice") or gas_fees.get("gasPrice") or gas_fees.get("maxFeePerGas") or 0
        )
        return max(0, native_after - native_before + gas_cost)

    async def _unwrap(self, client, adapter: DexAdapter, account, amount: int, gas_fees: dict) -> None:
        """WETH -> нативная монета. Неудача не критична: средства остаются в WETH."""
        try:
            nonce = await self.wallets.next_nonce(client, account.address)
            tx = await adapter.build_unwrap_tx(account.address, amount, nonce=nonce, gas_fees=gas_fees)
            sent = await self.wallets.send_tx(client, account, tx)
            await client.wait_receipt(sent.tx_hash, timeout=120)
            log.info("UNWRAP %s: %s", from_wei(amount), sent.tx_hash)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось развернуть WETH (%s) — средства остались в WETH: %s",
                        from_wei(amount), exc)

    async def _store_buy(self, user, chain_key, token, adapter, pool, spend_wei, received,
                         tx_hash, source, cfg, receipt, pair_address) -> int:
        chain = self.registry.config(chain_key)
        async with session_scope() as session:
            position = await repo.position_by_token(session, user.id, chain_key, token.address)
            if position is None:
                position = Position(
                    user_id=user.id, chain=chain_key, token_address=token.address,
                    token_symbol=token.symbol, token_decimals=token.decimals,
                    pair_address=pool.address or pair_address, router_address=adapter.router,
                    dex_kind=adapter.kind, pool_fee=pool.fee, source=source,
                    # Значения из mapped_column(default=...) появляются только при
                    # вставке в базу, а суммы накапливаются прямо сейчас: без явных
                    # нулей первая реальная покупка падала на «None + int», и позиция
                    # не записывалась, хотя монеты уже были потрачены.
                    amount_wei=0, bought_wei=0, native_spent_wei=0, native_returned_wei=0,
                )
                session.add(position)
            position.amount_wei = (position.amount_wei or 0) + received
            position.bought_wei = (position.bought_wei or 0) + received
            position.native_spent_wei = (position.native_spent_wei or 0) + spend_wei
            position.buy_tx = tx_hash
            position.status = "open"
            position.dex_kind = adapter.kind
            position.pool_fee = pool.fee
            position.pair_address = pool.address or position.pair_address
            position.router_address = adapter.router

            total_tokens = from_wei(position.amount_wei, token.decimals)
            entry = from_wei(spend_wei, chain.native_decimals) / from_wei(received, token.decimals)
            position.entry_price = (
                from_wei(position.native_spent_wei, chain.native_decimals) / total_tokens
                if total_tokens > 0 else entry
            )
            position.last_price = position.entry_price
            position.peak_price = max(position.peak_price or Decimal(0), position.entry_price or Decimal(0))
            position.take_profit_pct = cfg.take_profit_pct
            position.stop_loss_pct = cfg.stop_loss_pct
            position.trailing_stop_pct = cfg.trailing_stop_pct
            position.auto_sell = cfg.auto_sell
            position.sell_percent = cfg.sell_percent
            position.tp_ladder = cfg.tp_ladder or ""
            position.breakeven_pct = cfg.breakeven_pct
            position.rug_guard_pct = cfg.rug_guard_pct
            position.dead_timeout_min = cfg.dead_timeout_min
            position.dead_min_pct = cfg.dead_min_pct
            position.token_owner = token.owner
            await session.flush()
            position_id = position.id
            await repo.log_trade(
                session, user_id=user.id, position_id=position_id, chain=chain_key, kind="buy",
                token_address=token.address, amount_in_wei=spend_wei, amount_out_wei=received,
                tx_hash=tx_hash, status="success", gas_used=int(receipt.get("gasUsed", 0)),
            )
        return position_id

    async def _ensure_allowance(self, client, adapter: DexAdapter, account, token: str,
                                amount: int, cfg: ChainSettings, gas_fees: dict) -> None:
        spender = adapter.spender
        current = await allowance(client, token, account.address, spender)
        if current >= amount:
            return
        approve_amount = MAX_UINT256 if cfg.approve_max else amount
        nonce = await self.wallets.next_nonce(client, account.address)
        tx = await adapter.build_approve_tx(token, account.address, approve_amount,
                                            nonce=nonce, gas_fees=gas_fees)
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


def _revert_reason(exc: Exception) -> str:
    """Короткая причина отказа для сообщения пользователю."""
    text = str(exc)
    for marker in ("execution reverted:", "execution reverted"):
        if marker in text:
            tail = text.split(marker, 1)[1].strip(" '\";:")
            return tail[:120] or "контракт отклонил сделку"
    return text[:160]
