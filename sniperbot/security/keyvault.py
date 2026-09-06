"""Шифрование приватных ключей пользователей.

Схема: scrypt(MASTER_KEY, salt) -> 32-байтный ключ -> AES-256-GCM.
Каждая запись имеет собственную соль и nonce, поэтому одинаковые ключи
дают разные шифротексты. Формат хранения:

    v1:<base64( salt(16) || nonce(12) || ciphertext+tag )>

Потеря MASTER_KEY = безвозвратная потеря доступа к кошелькам.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

# Параметры scrypt: ~64 МБ памяти, компромисс между стойкостью и скоростью,
# т.к. расшифровка происходит на каждой торговой операции.
SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1


class VaultError(RuntimeError):
    """Ошибка шифрования/расшифровки приватного ключа."""


class KeyVault:
    """Шифрует и расшифровывает секреты одним мастер-ключом."""

    def __init__(self, master_key: str) -> None:
        if not master_key or len(master_key) < 16:
            raise VaultError("MASTER_KEY не задан или короче 16 символов")
        self._master = master_key.encode("utf-8")
        self._cache: dict[bytes, bytes] = {}

    def _derive(self, salt: bytes) -> bytes:
        cached = self._cache.get(salt)
        if cached is not None:
            return cached
        key = hashlib.scrypt(
            self._master, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES,
            maxmem=256 * 1024 * 1024,
        )
        # Кэш нужен, чтобы не пересчитывать scrypt на каждой сделке одного и того же кошелька.
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[salt] = key
        return key

    def encrypt(self, plaintext: str, *, aad: str | None = None) -> str:
        salt = secrets.token_bytes(SALT_BYTES)
        nonce = os.urandom(NONCE_BYTES)
        aesgcm = AESGCM(self._derive(salt))
        blob = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), _aad(aad))
        return f"{VERSION}:{base64.b64encode(salt + nonce + blob).decode('ascii')}"

    def decrypt(self, token: str, *, aad: str | None = None) -> str:
        if not token or ":" not in token:
            raise VaultError("Пустой или повреждённый шифротекст")
        version, _, payload = token.partition(":")
        if version != VERSION:
            raise VaultError(f"Неподдерживаемая версия шифрования: {version}")
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception as exc:  # noqa: BLE001 - любые ошибки base64 равнозначны
            raise VaultError("Шифротекст повреждён") from exc
        if len(raw) < SALT_BYTES + NONCE_BYTES + 16:
            raise VaultError("Шифротекст слишком короткий")
        salt, nonce, blob = (
            raw[:SALT_BYTES],
            raw[SALT_BYTES : SALT_BYTES + NONCE_BYTES],
            raw[SALT_BYTES + NONCE_BYTES :],
        )
        aesgcm = AESGCM(self._derive(salt))
        try:
            return aesgcm.decrypt(nonce, blob, _aad(aad)).decode("utf-8")
        except InvalidTag as exc:
            raise VaultError(
                "Не удалось расшифровать ключ: неверный MASTER_KEY или изменённые данные"
            ) from exc

    def fingerprint(self) -> str:
        """Короткий отпечаток мастер-ключа — чтобы заметить его подмену."""
        digest = hmac.new(self._master, b"sniperbot-master-fingerprint", hashlib.sha256).hexdigest()
        return digest[:12]


def _aad(aad: str | None) -> bytes | None:
    return aad.encode("utf-8") if aad else None
