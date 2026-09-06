"""Операции с БД, которыми пользуются хендлеры и фоновые задачи."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sniperbot.db.models import (
    ChainSettings,
    Position,
    ScannerState,
    SeenPair,
    TokenFlag,
    TradeLog,
    User,
    WalletEvent,
    utcnow,
)


# --------------------------------------------------------------------------- users
async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_or_create_user(
    session: AsyncSession, user_id: int, username: str | None = None, default_chain: str = "bsc"
) -> tuple[User, bool]:
    user = await session.get(User, user_id)
    if user is not None:
        created = False
        if username and user.username != username:
            user.username = username
        user.last_seen_at = utcnow()
    else:
        created = True
        user = User(id=user_id, username=username, active_chain=default_chain)
        session.add(user)
        await session.flush()
    return user, created


async def all_users(session: AsyncSession, *, with_wallet: bool = True) -> list[User]:
    stmt = select(User).where(User.is_blocked.is_(False))
    if with_wallet:
        stmt = stmt.where(User.wallet_address.is_not(None))
    return list((await session.scalars(stmt)).all())


# ----------------------------------------------------------------------- settings
async def get_settings(session: AsyncSession, user_id: int, chain: str) -> ChainSettings:
    stmt = select(ChainSettings).where(
        ChainSettings.user_id == user_id, ChainSettings.chain == chain
    )
    settings = await session.scalar(stmt)
    if settings is None:
        settings = ChainSettings(user_id=user_id, chain=chain)
        session.add(settings)
        await session.flush()
    return settings


async def users_with_autosnipe(session: AsyncSession, chain: str) -> list[tuple[User, ChainSettings]]:
    stmt = (
        select(User, ChainSettings)
        .join(ChainSettings, ChainSettings.user_id == User.id)
        .where(
            ChainSettings.chain == chain,
            ChainSettings.auto_snipe.is_(True),
            User.is_blocked.is_(False),
            User.wallet_address.is_not(None),
        )
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


# ---------------------------------------------------------------------- positions
async def open_positions(
    session: AsyncSession, *, user_id: int | None = None, chain: str | None = None
) -> list[Position]:
    stmt = select(Position).where(Position.status == "open")
    if user_id is not None:
        stmt = stmt.where(Position.user_id == user_id)
    if chain is not None:
        stmt = stmt.where(Position.chain == chain)
    return list((await session.scalars(stmt.order_by(Position.opened_at.desc()))).all())


async def count_open_positions(session: AsyncSession, user_id: int, chain: str) -> int:
    stmt = (
        select(func.count())
        .select_from(Position)
        .where(Position.user_id == user_id, Position.chain == chain, Position.status == "open")
    )
    return int(await session.scalar(stmt) or 0)


async def find_position(session: AsyncSession, position_id: int, user_id: int | None = None) -> Position | None:
    position = await session.get(Position, position_id)
    if position is None:
        return None
    if user_id is not None and position.user_id != user_id:
        return None
    return position


async def position_by_token(
    session: AsyncSession, user_id: int, chain: str, token: str
) -> Position | None:
    stmt = select(Position).where(
        Position.user_id == user_id,
        Position.chain == chain,
        func.lower(Position.token_address) == token.lower(),
        Position.status == "open",
    )
    return await session.scalar(stmt)


async def closed_positions(session: AsyncSession, user_id: int, limit: int = 10) -> list[Position]:
    stmt = (
        select(Position)
        .where(Position.user_id == user_id, Position.status != "open")
        .order_by(Position.closed_at.desc().nullslast(), Position.id.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def snipes_last_hour(session: AsyncSession, user_id: int, chain: str) -> int:
    since = utcnow() - dt.timedelta(hours=1)
    stmt = (
        select(func.count())
        .select_from(Position)
        .where(
            Position.user_id == user_id,
            Position.chain == chain,
            Position.source == "auto",
            Position.opened_at >= since,
        )
    )
    return int(await session.scalar(stmt) or 0)


# --------------------------------------------------------------------------- logs
async def log_trade(session: AsyncSession, **kwargs) -> TradeLog:
    entry = TradeLog(**kwargs)
    session.add(entry)
    await session.flush()
    return entry


async def log_wallet_event(session: AsyncSession, **kwargs) -> WalletEvent:
    event = WalletEvent(**kwargs)
    session.add(event)
    await session.flush()
    return event


async def recent_wallet_events(session: AsyncSession, user_id: int, limit: int = 10) -> list[WalletEvent]:
    stmt = (
        select(WalletEvent)
        .where(WalletEvent.user_id == user_id)
        .order_by(WalletEvent.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# ------------------------------------------------------------------------- pairs
async def seen_pair_exists(session: AsyncSession, chain: str, pair_address: str) -> bool:
    stmt = select(SeenPair.id).where(
        SeenPair.chain == chain, func.lower(SeenPair.pair_address) == pair_address.lower()
    )
    return await session.scalar(stmt) is not None


async def add_seen_pair(session: AsyncSession, **kwargs) -> SeenPair:
    pair = SeenPair(**kwargs)
    session.add(pair)
    await session.flush()
    return pair


async def mark_pair(session: AsyncSession, pair_id: int, status: str, reason: str | None = None) -> None:
    await session.execute(
        update(SeenPair).where(SeenPair.id == pair_id).values(status=status, reason=reason)
    )


async def recent_pairs(session: AsyncSession, chain: str, limit: int = 10) -> list[SeenPair]:
    stmt = (
        select(SeenPair)
        .where(SeenPair.chain == chain)
        .order_by(SeenPair.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


# ----------------------------------------------------------------- scanner state
async def get_scanner_state(session: AsyncSession, chain: str, factory: str) -> ScannerState:
    stmt = select(ScannerState).where(
        ScannerState.chain == chain, func.lower(ScannerState.factory) == factory.lower()
    )
    state = await session.scalar(stmt)
    if state is None:
        state = ScannerState(chain=chain, factory=factory, last_block=0)
        session.add(state)
        await session.flush()
    return state


# ------------------------------------------------------------------------- flags
async def is_blacklisted(session: AsyncSession, chain: str, token: str, user_id: int | None) -> bool:
    stmt = select(TokenFlag.id).where(
        TokenFlag.chain == chain,
        func.lower(TokenFlag.token_address) == token.lower(),
        TokenFlag.kind == "blacklist",
        TokenFlag.user_id.in_([user_id, None]) if user_id is not None else TokenFlag.user_id.is_(None),
    )
    return await session.scalar(stmt) is not None


async def add_flag(
    session: AsyncSession, chain: str, token: str, kind: str, user_id: int | None, note: str | None = None
) -> None:
    existing = await session.scalar(
        select(TokenFlag).where(
            TokenFlag.chain == chain,
            func.lower(TokenFlag.token_address) == token.lower(),
            TokenFlag.user_id.is_(user_id) if user_id is None else TokenFlag.user_id == user_id,
        )
    )
    if existing is not None:
        existing.kind = kind
        existing.note = note
        return
    session.add(TokenFlag(chain=chain, token_address=token, kind=kind, user_id=user_id, note=note))


async def total_pnl(session: AsyncSession, user_id: int, chain: str | None = None) -> tuple[int, int]:
    """Сумма потраченного и возвращённого нативного токена по закрытым позициям."""
    stmt = select(Position).where(Position.user_id == user_id, Position.status == "closed")
    if chain:
        stmt = stmt.where(Position.chain == chain)
    positions = list((await session.scalars(stmt)).all())
    spent = sum(p.native_spent_wei for p in positions)
    returned = sum(p.native_returned_wei for p in positions)
    return spent, returned


def pnl_pct(spent: int, returned: int) -> Decimal | None:
    if spent <= 0:
        return None
    return (Decimal(returned - spent) / Decimal(spent)) * 100
