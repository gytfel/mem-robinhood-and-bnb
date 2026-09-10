"""Хранилище: пользователи, настройки, позиции, пары."""

from __future__ import annotations

from decimal import Decimal

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position


async def test_user_and_settings_created_once(db):
    async with session_scope() as session:
        user, created = await repo.get_or_create_user(session, 100, "vlad")
        assert created is True
        again, created_again = await repo.get_or_create_user(session, 100, "vlad")
        assert created_again is False
        assert again.id == user.id

        cfg = await repo.get_settings(session, 100, "bsc")
        assert cfg.slippage_bps == 1500
        assert cfg.buy_amount == Decimal("0.01")
        assert cfg.auto_snipe is False


async def test_settings_are_per_chain(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        bsc = await repo.get_settings(session, 1, "bsc")
        rh = await repo.get_settings(session, 1, "robinhood")
        bsc.buy_amount = Decimal("0.5")

    async with session_scope() as session:
        assert (await repo.get_settings(session, 1, "bsc")).buy_amount == Decimal("0.5")
        assert (await repo.get_settings(session, 1, "robinhood")).buy_amount == Decimal("0.01")
        assert rh.chain == "robinhood"


async def test_autosnipe_subscribers_filtered(db):
    async with session_scope() as session:
        for uid in (1, 2, 3):
            user, _ = await repo.get_or_create_user(session, uid)
            user.wallet_address = f"0x{uid:040x}"
            cfg = await repo.get_settings(session, uid, "bsc")
            cfg.auto_snipe = uid != 2
        blocked, _ = await repo.get_or_create_user(session, 3)
        blocked.is_blocked = True

    async with session_scope() as session:
        subscribers = await repo.users_with_autosnipe(session, "bsc")
        assert [user.id for user, _ in subscribers] == [1]


async def test_position_lifecycle_and_pnl(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 7)
        session.add(
            Position(
                user_id=7, chain="bsc", token_address="0xabc", token_symbol="MEME",
                router_address="0xr", amount_wei=10**18, native_spent_wei=10**17,
                entry_price=Decimal("0.1"), status="open",
            )
        )

    async with session_scope() as session:
        assert await repo.count_open_positions(session, 7, "bsc") == 1
        position = (await repo.open_positions(session, user_id=7))[0]
        position.native_returned_wei = 3 * 10**17
        position.status = "closed"
        position.amount_wei = 0

    async with session_scope() as session:
        assert await repo.count_open_positions(session, 7, "bsc") == 0
        spent, returned = await repo.total_pnl(session, 7)
        assert spent == 10**17
        assert returned == 3 * 10**17
        assert repo.pnl_pct(spent, returned) == Decimal(200)
        assert repo.pnl_pct(0, 0) is None


async def test_seen_pairs_are_deduplicated(db):
    async with session_scope() as session:
        assert await repo.seen_pair_exists(session, "bsc", "0xPAIR") is False
        record = await repo.add_seen_pair(
            session, chain="bsc", pair_address="0xPAIR", token_address="0xTOKEN", block_number=1
        )
        assert record.id is not None

    async with session_scope() as session:
        # регистр адреса не должен создавать дубль
        assert await repo.seen_pair_exists(session, "bsc", "0xpair") is True
        await repo.mark_pair(session, record.id, "rejected", "низкая ликвидность")

    async with session_scope() as session:
        pairs = await repo.recent_pairs(session, "bsc")
        assert pairs[0].status == "rejected"
        assert pairs[0].reason == "низкая ликвидность"


async def test_scanner_cursor_roundtrip(db):
    async with session_scope() as session:
        state = await repo.get_scanner_state(session, "bsc", "0xFACTORY")
        assert state.last_block == 0
        state.last_block = 4242

    async with session_scope() as session:
        again = await repo.get_scanner_state(session, "bsc", "0xfactory")
        assert again.last_block == 4242


async def test_blacklist(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 5)
        assert await repo.is_blacklisted(session, "bsc", "0xBAD", 5) is False
        await repo.add_flag(session, "bsc", "0xBAD", "blacklist", 5, "скам")

    async with session_scope() as session:
        assert await repo.is_blacklisted(session, "bsc", "0xbad", 5) is True
        assert await repo.is_blacklisted(session, "bsc", "0xbad", 6) is False


async def test_wei_column_survives_huge_values(db):
    """uint256 не влезает в int64 — проверяем, что хранение строкой работает."""
    huge = 2**200
    async with session_scope() as session:
        await repo.get_or_create_user(session, 8)
        session.add(
            Position(user_id=8, chain="bsc", token_address="0x1", router_address="0x2",
                     amount_wei=huge, native_spent_wei=1, status="open")
        )

    async with session_scope() as session:
        position = (await repo.open_positions(session, user_id=8))[0]
        assert position.amount_wei == huge


async def test_migration_adds_missing_indexes_not_only_columns(db):
    """ALTER TABLE ADD COLUMN индекс не создаёт, а create_all() старые таблицы не трогает.

    Без этого обновлённая база и свежая расходятся: один и тот же запрос в одной
    идёт по индексу, в другой — перебором.
    """
    from sqlalchemy import inspect

    from sniperbot.db.base import session_factory

    async with session_factory()() as session:
        names = await session.run_sync(
            lambda sync: {index["name"] for index in inspect(sync.bind).get_indexes("users")}
        )

    assert "ix_users_referred_by" in names       # добавлен вместе со столбцом
    assert "ix_users_wallet_address" in names
