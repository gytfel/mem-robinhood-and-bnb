"""Общие фикстуры тестов."""

from __future__ import annotations

import pytest

from sniperbot.db.base import close_db, init_db


@pytest.fixture
async def db():
    """Чистая in-memory база на каждый тест."""
    await init_db("sqlite+aiosqlite:///:memory:")
    yield
    await close_db()


@pytest.fixture
def vault():
    from sniperbot.security.keyvault import KeyVault

    return KeyVault("test-master-key-" + "x" * 32)
