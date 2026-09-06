import pytest

from sniperbot.security.keyvault import KeyVault, VaultError


def test_roundtrip(vault):
    secret = "0x" + "ab" * 32
    token = vault.encrypt(secret, aad="42")
    assert token.startswith("v1:")
    assert vault.decrypt(token, aad="42") == secret


def test_same_plaintext_gives_different_ciphertext(vault):
    a = vault.encrypt("secret", aad="1")
    b = vault.encrypt("secret", aad="1")
    assert a != b


def test_wrong_master_key_fails(vault):
    token = vault.encrypt("secret", aad="1")
    other = KeyVault("another-master-key-" + "y" * 32)
    with pytest.raises(VaultError):
        other.decrypt(token, aad="1")


def test_aad_binds_ciphertext_to_user(vault):
    """Ключ пользователя 1 нельзя расшифровать как ключ пользователя 2."""
    token = vault.encrypt("secret", aad="1")
    with pytest.raises(VaultError):
        vault.decrypt(token, aad="2")


def test_tampered_ciphertext_rejected(vault):
    token = vault.encrypt("secret", aad="1")
    body = list(token.split(":", 1)[1])
    body[-4] = "A" if body[-4] != "A" else "B"
    with pytest.raises(VaultError):
        vault.decrypt("v1:" + "".join(body), aad="1")


def test_short_master_key_rejected():
    with pytest.raises(VaultError):
        KeyVault("short")


def test_unknown_version_rejected(vault):
    with pytest.raises(VaultError):
        vault.decrypt("v9:AAAA", aad="1")
