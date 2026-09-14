"""Кошельки пользователей: генерация, шифрование, подпись и отправка транзакций."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from decimal import Decimal

from eth_account import Account
from eth_account.signers.local import LocalAccount

from sniperbot.chain.clients import ChainClient
from sniperbot.db.models import User
from sniperbot.security.keyvault import KeyVault, VaultError
from sniperbot.utils.evm import to_checksum
from sniperbot.utils.fmt import from_wei

log = logging.getLogger(__name__)

# Резерв нативной монеты на газ при выводе «всего баланса».
NATIVE_TRANSFER_GAS = 21_000


class WalletError(RuntimeError):
    """Ошибка операции с кошельком."""


@dataclass(slots=True)
class SentTx:
    tx_hash: str
    nonce: int


class NonceManager:
    """Последовательные nonce для одного адреса в одной сети."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._next: dict[tuple[str, str], int] = {}

    def lock(self, chain: str, address: str) -> asyncio.Lock:
        key = (chain, address.lower())
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def reserve(self, client: ChainClient, address: str) -> int:
        key = (client.config.key, address.lower())
        on_chain = await client.transaction_count(address, "pending")
        nonce = max(on_chain, self._next.get(key, 0))
        self._next[key] = nonce + 1
        return nonce

    def reset(self, chain: str, address: str) -> None:
        self._next.pop((chain, address.lower()), None)


class WalletService:
    """Создание кошельков и отправка подписанных транзакций."""

    def __init__(self, vault: KeyVault) -> None:
        self.vault = vault
        self.nonces = NonceManager()

    # ------------------------------------------------------------- создание
    def generate(self, user_id: int | str) -> tuple[str, str]:
        """Новый кошелёк: возвращает (адрес, зашифрованный приватный ключ)."""
        account: LocalAccount = Account.from_key(secrets.token_bytes(32))
        encrypted = self.vault.encrypt(account.key.hex(), aad=str(user_id))
        return to_checksum(account.address), encrypted

    def import_key(self, user_id: int, private_key: str) -> tuple[str, str]:
        key = private_key.strip()
        if not key.startswith("0x"):
            key = "0x" + key
        try:
            account: LocalAccount = Account.from_key(key)
        except Exception as exc:  # noqa: BLE001
            raise WalletError("Некорректный приватный ключ") from exc
        return to_checksum(account.address), self.vault.encrypt(account.key.hex(), aad=str(user_id))

    def ensure_wallet(self, user: User) -> bool:
        """Создаёт кошелёк, если у пользователя его ещё нет. True — если создан."""
        if user.wallet_address and user.encrypted_key:
            return False
        address, encrypted = self.generate(user.id)
        user.wallet_address = address
        user.encrypted_key = encrypted
        user.key_fingerprint = self.vault.fingerprint()
        log.info("Создан кошелёк для пользователя %s: %s", user.id, address)
        return True

    def account(self, user: User) -> LocalAccount:
        if not user.encrypted_key:
            raise WalletError("У пользователя нет кошелька")
        return self.account_from(user.encrypted_key, str(user.id))

    def account_from(self, encrypted: str, aad: str) -> LocalAccount:
        """Аккаунт по зашифрованному ключу. Служебные кошельки живут не в users."""
        try:
            private_key = self.vault.decrypt(encrypted, aad=aad)
        except VaultError as exc:
            raise WalletError(str(exc)) from exc
        return Account.from_key(private_key)

    def export_key(self, user: User) -> str:
        return self.account(user).key.hex()

    # ------------------------------------------------------------- отправка
    async def send_tx(self, client: ChainClient, account: LocalAccount, tx: dict) -> SentTx:
        """Подписывает и отправляет транзакцию, управляя nonce.

        Номер лучше не готовить заранее: занятый и неотправленный nonce
        оставляет в нумерации дыру, и на следующую транзакцию узел отвечает
        «nonce too high». Поэтому вызывающий код передаёт транзакцию без
        nonce, а номер выдаётся здесь — под замком, прямо перед подписью.
        """
        address = to_checksum(account.address)
        async with self.nonces.lock(client.config.key, address):
            tx_hash = ""
            for attempt in (1, 2):
                if "nonce" not in tx:
                    tx["nonce"] = await self.nonces.reserve(client, address)
                tx.setdefault("chainId", client.config.chain_id)
                signed = account.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
                try:
                    tx_hash = await client.send_raw(raw)
                    break
                except Exception as exc:  # noqa: BLE001
                    # Сбрасываем локальный счётчик, иначе рассинхрон с сетью.
                    self.nonces.reset(client.config.key, address)
                    message = str(exc)
                    lowered = message.lower()
                    if "nonce too high" in lowered and attempt == 1:
                        # Счётчик ушёл вперёд сети. Эта транзакция точно не
                        # отправлена — в отличие от «nonce too low», где она
                        # может быть уже в блоке и повтор означал бы вторую
                        # такую же сделку. Перечитываем номер у сети и
                        # пробуем ещё раз: терять выход из позиции из-за
                        # дыры в нумерации нельзя.
                        log.warning("Nonce ушёл вперёд сети (%s) — пересинхронизирую и повторяю",
                                    message[:120])
                        del tx["nonce"]
                        continue
                    if "nonce too low" in lowered:
                        raise WalletError("Nonce устарел, повторите операцию") from exc
                    if "nonce too high" in lowered:
                        raise WalletError(
                            "Сеть ещё не приняла предыдущую транзакцию, счётчик ушёл вперёд. "
                            "Повторите через минуту."
                        ) from exc
                    if "insufficient funds" in lowered:
                        raise WalletError("Недостаточно средств на газ/сумму сделки") from exc
                    raise WalletError(f"RPC отклонил транзакцию: {message[:200]}") from exc
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return SentTx(tx_hash=tx_hash, nonce=int(tx["nonce"]))

    async def probe_nonce(self, client: ChainClient, address: str) -> int:
        """Номер для сборки и симуляции — без резерва.

        eth_call на него не смотрит, а оценка газа у некоторых узлов требует
        правдоподобного значения. Настоящий номер выдаст send_tx.
        """
        try:
            return await client.transaction_count(to_checksum(address), "pending")
        except Exception as exc:  # noqa: BLE001 - для симуляции сгодится и ноль
            log.debug("Не смог прочитать nonce для %s: %s", address, exc)
            return 0

    # -------------------------------------------------------------- балансы
    async def native_balance(self, client: ChainClient, address: str) -> int:
        return await client.native_balance(address)

    async def send_native(
        self,
        client: ChainClient,
        account: LocalAccount,
        to: str,
        amount_wei: int | None,
        *,
        gas_multiplier: Decimal | float = 1.1,
    ) -> tuple[SentTx, int]:
        """Перевод нативной монеты. amount_wei=None — вывести всё за вычетом газа."""
        balance = await client.native_balance(account.address)
        fees = await client.gas_fees(float(gas_multiplier))
        gas_price = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0)
        gas_cost = gas_price * NATIVE_TRANSFER_GAS

        if amount_wei is None:
            amount_wei = balance - gas_cost
            if amount_wei <= 0:
                raise WalletError(
                    f"На балансе {from_wei(balance):.8f} — не хватает даже на газ "
                    f"({from_wei(gas_cost):.8f} {client.config.native_symbol})"
                )
        elif amount_wei + gas_cost > balance:
            raise WalletError(
                f"Недостаточно средств: нужно {from_wei(amount_wei + gas_cost):.6f}, "
                f"есть {from_wei(balance):.6f} {client.config.native_symbol}"
            )

        tx = {
            "to": to_checksum(to),
            "value": int(amount_wei),
            "gas": NATIVE_TRANSFER_GAS,
            "chainId": client.config.chain_id,
            **fees,
        }
        sent = await self.send_tx(client, account, tx)
        return sent, int(amount_wei)
