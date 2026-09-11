"""Кошелёк комиссий: создание, ключ, отделённость от кошельков пользователей."""

from __future__ import annotations

import pytest

from sniperbot import treasury
from sniperbot.chain.wallet import WalletError, WalletService
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.security.keyvault import KeyVault

MASTER = "test-master-key-" + "x" * 32


def wallets() -> WalletService:
    return WalletService(KeyVault(MASTER))


async def test_wallet_is_created_once_and_never_replaced(db):
    """Перезапись означала бы потерю доступа к уже накопленным комиссиям."""
    service = wallets()
    first = await treasury.create(service)
    assert first.address.startswith("0x") and len(first.address) == 42

    second = await treasury.create(service)
    assert second.address == first.address
    assert second.encrypted == first.encrypted


async def test_no_wallet_until_it_is_asked_for(db):
    assert await treasury.get() is None


async def test_key_is_stored_encrypted_and_opens_the_same_address(db):
    service = wallets()
    created = await treasury.create(service)

    async with session_scope() as session:
        stored = await repo.get_state(session, treasury.STATE_KEY)
    assert stored and "0x" not in stored[:4], "ключ не должен лежать открытым"

    account = treasury.account(service, await treasury.get())
    assert account.address.lower() == created.address.lower()


async def test_another_master_key_cannot_open_it(db):
    """Шифрование привязано к MASTER_KEY — чужая копия базы бесполезна."""
    await treasury.create(wallets())
    stranger = WalletService(KeyVault("another-master-key-" + "y" * 32))
    with pytest.raises(WalletError):
        treasury.account(stranger, await treasury.get())


async def test_it_does_not_show_up_among_users(db):
    """Иначе кошелёк сервиса попадал бы в /users, рассылки и отчёты."""
    await treasury.create(wallets())
    async with session_scope() as session:
        assert await repo.user_count(session) == 0
        assert await repo.all_users(session, with_wallet=False) == []


async def test_user_wallets_stay_separate(db):
    """Разные владельцы — разные ключи: привязка шифрования у них не совпадает."""
    service = wallets()
    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 555)
        service.ensure_wallet(user)
        user_address = user.wallet_address
        user_encrypted = user.encrypted_key

    wallet = await treasury.create(service)
    assert wallet.address.lower() != user_address.lower()

    # Ключ пользователя нельзя открыть как служебный и наоборот.
    with pytest.raises(WalletError):
        service.account_from(user_encrypted, treasury.AAD)
    with pytest.raises(WalletError):
        service.account_from(wallet.encrypted, "555")
