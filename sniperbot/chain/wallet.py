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
    def generate(self, user_id: int) -> tuple[str, str]:
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
        try:
            private_key = self.vault.decrypt(user.encrypted_key, aad=str(user.id))
        except VaultError as exc:
            raise WalletError(str(exc)) from exc
        return Account.from_key(private_key)

    def export_key(self, user: User) -> str:
        return self.account(user).key.hex()

    # ------------------------------------------------------------- отправка
    async def send_tx(self, client: ChainClient, account: LocalAccount, tx: dict) -> SentTx:
        """Подписывает и отправляет транзакцию, управляя nonce."""
        address = to_checksum(account.address)
        async with self.nonces.lock(client.config.key, address):
            if "nonce" not in tx:
                tx["nonce"] = await self.nonces.reserve(client, address)
            tx.setdefault("chainId", client.config.chain_id)
            signed = account.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            try:
                tx_hash = await client.send_raw(raw)
            except Exception as exc:  # noqa: BLE001
                # Сбрасываем локальный счётчик, иначе рассинхрон с сетью.
                self.nonces.reset(client.config.key, address)
                message = str(exc)
                if "nonce too low" in message.lower():
                    raise WalletError("Nonce устарел, повторите операцию") from exc
                if "insufficient funds" in message.lower():
                    raise WalletError("Недостаточно средств на газ/сумму сделки") from exc
                raise WalletError(f"RPC отклонил транзакцию: {message[:200]}") from exc
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return SentTx(tx_hash=tx_hash, nonce=int(tx["nonce"]))

    async def next_nonce(self, client: ChainClient, address: str) -> int:
        async with self.nonces.lock(client.config.key, address):
            return await self.nonces.reserve(client, address)

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
