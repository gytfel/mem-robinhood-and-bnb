"""Кошелёк для комиссий — внутри бота, отдельно от кошельков пользователей.

Комиссии можно отправлять на любой внешний адрес, но тогда владельцу нужен
второй кошелёк где-то ещё. Здесь бот заводит адрес сам: ключ шифруется тем же
MASTER_KEY, что и ключи пользователей, и лежит в служебной таблице, а не среди
них — иначе кошелёк сервиса попадал бы в /users, рассылки и отчёты.

Ключ отдаётся по первому требованию (`/treasury key`): деньги, которые нельзя
забрать без работающего бота, — это не ваши деньги.
"""

from __future__ import annotations

from dataclasses import dataclass

from sniperbot.db import repo
from sniperbot.db.base import session_scope

STATE_ADDRESS = "treasury_address"
STATE_KEY = "treasury_key"
AAD = "treasury"        # привязка шифрования: у пользователей это их id


@dataclass(slots=True)
class Treasury:
    """Адрес сбора комиссий и зашифрованный ключ к нему."""

    address: str
    encrypted: str


async def get() -> Treasury | None:
    """Кошелёк комиссий, если он уже заведён."""
    async with session_scope() as session:
        address = await repo.get_state(session, STATE_ADDRESS)
        encrypted = await repo.get_state(session, STATE_KEY)
    return Treasury(address, encrypted) if address and encrypted else None


async def create(wallets) -> Treasury:  # noqa: ANN001 - WalletService, без кольцевого импорта
    """Заводит кошелёк комиссий. Существующий не перезаписывает.

    Перезапись означала бы потерю доступа к уже накопленному: ключ один, и
    второго шанса у владельца не будет.
    """
    existing = await get()
    if existing is not None:
        return existing
    address, encrypted = wallets.generate(AAD)
    async with session_scope() as session:
        await repo.set_state(session, STATE_ADDRESS, address)
        await repo.set_state(session, STATE_KEY, encrypted)
    return Treasury(address, encrypted)


def account(wallets, treasury: Treasury):  # noqa: ANN001, ANN201 - LocalAccount
    """Подписывающий аккаунт кошелька комиссий."""
    return wallets.account_from(treasury.encrypted, AAD)
