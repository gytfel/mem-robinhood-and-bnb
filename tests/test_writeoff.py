"""Списание позиции, которую невозможно продать."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position
from sniperbot.reports import EXIT_TITLES, summarize
from sniperbot.utils.fmt import to_wei

TOKEN = "0x55d398326f99059fF775485246999027B3197955"


async def stuck_position(**kwargs) -> int:
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        defaults = {
            "user_id": 1, "chain": "rh", "token_address": TOKEN, "token_symbol": "FLYBOOK",
            "router_address": "0x" + "r" * 40, "status": "open",
            "amount_wei": to_wei(100), "bought_wei": to_wei(100),
            "native_spent_wei": to_wei("0.001"), "native_returned_wei": 0,
        }
        defaults.update(kwargs)
        position = Position(**defaults)
        session.add(position)
        await session.flush()
        return position.id


async def test_written_off_position_leaves_the_active_list(db):
    """Ради этого всё и делается: позиция не занимает лимит и не дёргает монитор."""
    position_id = await stuck_position()
    async with session_scope() as session:
        assert await repo.count_open_positions(session, 1, "rh") == 1

    async with session_scope() as session:
        assert await repo.write_off_position(session, position_id, 1) is not None

    async with session_scope() as session:
        assert await repo.count_open_positions(session, 1, "rh") == 0
        assert await repo.open_positions(session, user_id=1) == []


async def test_the_loss_stays_in_the_reports(db):
    """Деньги потрачены — прятать их из статистики значит обманывать себя."""
    position_id = await stuck_position()
    async with session_scope() as session:
        await repo.write_off_position(session, position_id, 1)

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    async with session_scope() as session:
        rows = await repo.closed_between(session, 1, since, paper=False)

    summary = summarize(rows, "боевые")
    assert summary.count == 1
    assert summary.pnl == Decimal("-0.001")
    assert rows[0].exit_reason == "stuck"
    assert "продать невозможно" in EXIT_TITLES["stuck"]


async def test_a_written_off_position_can_come_back(db):
    """Токен может ожить — /recover возвращает и «утраченные», и «непродаваемые»."""
    position_id = await stuck_position()
    async with session_scope() as session:
        await repo.write_off_position(session, position_id, 1)

    async with session_scope() as session:
        found = await repo.lost_position_by_token(session, 1, "rh", TOKEN)
    assert found is not None and found.id == position_id


async def test_only_the_owner_can_write_off_and_only_once(db):
    position_id = await stuck_position()
    async with session_scope() as session:
        assert await repo.write_off_position(session, position_id, 999) is None   # чужая
    async with session_scope() as session:
        assert await repo.write_off_position(session, position_id, 1) is not None
    async with session_scope() as session:
        assert await repo.write_off_position(session, position_id, 1) is None     # уже закрыта


async def test_unknown_position_is_not_an_error(db):
    async with session_scope() as session:
        assert await repo.write_off_position(session, 4242, 1) is None
