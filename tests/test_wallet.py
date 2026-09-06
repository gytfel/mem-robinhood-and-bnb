from eth_account import Account

from sniperbot.chain.wallet import WalletService
from sniperbot.db.models import User


def test_generated_wallet_matches_encrypted_key(vault):
    service = WalletService(vault)
    address, encrypted = service.generate(777)
    restored = Account.from_key(vault.decrypt(encrypted, aad="777"))
    assert restored.address == address


def test_ensure_wallet_is_idempotent(vault):
    service = WalletService(vault)
    user = User(id=5)
    assert service.ensure_wallet(user) is True
    address = user.wallet_address
    assert service.ensure_wallet(user) is False
    assert user.wallet_address == address


def test_import_key_roundtrip(vault):
    service = WalletService(vault)
    account = Account.create()
    address, encrypted = service.import_key(9, account.key.hex())
    assert address == account.address
    assert vault.decrypt(encrypted, aad="9").lstrip("0x") == account.key.hex().lstrip("0x")


def test_account_decrypts_for_owner(vault):
    service = WalletService(vault)
    user = User(id=11)
    service.ensure_wallet(user)
    assert service.account(user).address == user.wallet_address
