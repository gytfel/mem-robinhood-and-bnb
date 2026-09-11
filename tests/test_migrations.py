"""Обновление живой базы: новые поля появляются, старые данные остаются.

На сервере база не пересоздаётся — в ней позиции, ключи и история. Каждая новая
настройка добавляет столбец в уже существующую таблицу, и цена ошибки здесь —
потерянные данные пользователя, а не упавший тест.
"""

from __future__ import annotations

import sqlite3

import pytest

from sniperbot.db.base import close_db, init_db, session_scope
from sniperbot.db.models import Position


@pytest.fixture
async def legacy_db(tmp_path):
    """База прошлой версии: та же таблица позиций, но без новых столбцов."""
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            chain VARCHAR(32) NOT NULL,
            token_address VARCHAR(42) NOT NULL,
            token_symbol VARCHAR(32) DEFAULT '?',
            token_decimals INTEGER DEFAULT 18,
            router_address VARCHAR(42) NOT NULL,
            amount_wei VARCHAR DEFAULT '0',
            bought_wei VARCHAR DEFAULT '0',
            native_spent_wei VARCHAR DEFAULT '0',
            native_returned_wei VARCHAR DEFAULT '0',
            status VARCHAR(16) DEFAULT 'open',
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO positions (id, user_id, chain, token_address, router_address,
                               amount_wei, native_spent_wei, status)
        VALUES (7, 555, 'rh', '0xToken', '0xRouter', '1000', '1000', 'open');
        """
    )
    connection.commit()
    connection.close()

    await init_db(f"sqlite+aiosqlite:///{path}")
    yield path
    await close_db()


async def test_new_columns_appear_and_the_row_survives(legacy_db):
    async with session_scope() as session:
        position = await session.get(Position, 7)

    assert position is not None, "старая позиция потерялась при обновлении"
    assert position.token_address == "0xToken"
    assert int(position.native_spent_wei) == 1000
    # Поля, добавленные в этой версии, читаются со значением по умолчанию.
    assert position.secure_pct == 0
    assert position.tp_ladder == ""
    assert position.exit_reason == ""


async def test_new_tables_appear_next_to_the_old_ones(legacy_db):
    from sniperbot.db import repo

    async with session_scope() as session:
        await repo.set_state(session, "проверка", "значение")
    async with session_scope() as session:
        assert await repo.get_state(session, "проверка") == "значение"


async def test_running_the_migration_twice_changes_nothing(legacy_db):
    """Перезапуск службы не должен ломать то, что уже мигрировано."""
    await close_db()                     # как при остановке службы
    await init_db(f"sqlite+aiosqlite:///{legacy_db}")
    async with session_scope() as session:
        assert await session.get(Position, 7) is not None
