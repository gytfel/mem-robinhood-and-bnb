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


# ------------------------------------------------------------------ риск-лимиты
async def last_position_at(
    session: AsyncSession, user_id: int, chain: str, source: str | None = None
) -> dt.datetime | None:
    stmt = select(func.max(Position.opened_at)).where(
        Position.user_id == user_id, Position.chain == chain
    )
    if source:
        stmt = stmt.where(Position.source == source)
    return await session.scalar(stmt)


async def realized_pnl_since(
    session: AsyncSession, user_id: int, chain: str | None, since: dt.datetime,
    paper: bool | None = None,
) -> int:
    """Реализованный P&L (в wei) по закрытым позициям с момента `since`."""
    stmt = select(Position).where(
        Position.user_id == user_id,
        Position.status == "closed",
        Position.closed_at >= since,
    )
    if chain:
        stmt = stmt.where(Position.chain == chain)
    if paper is not None:
        stmt = stmt.where(Position.is_paper.is_(paper))
    positions = list((await session.scalars(stmt)).all())
    return sum(p.native_returned_wei - p.native_spent_wei for p in positions)


async def consecutive_losses(
    session: AsyncSession, user_id: int, chain: str, since: dt.datetime | None = None
) -> int:
    """Сколько последних закрытых сделок подряд оказались убыточными."""
    stmt = (
        select(Position)
        .where(Position.user_id == user_id, Position.chain == chain, Position.status == "closed")
        .order_by(Position.closed_at.desc().nullslast(), Position.id.desc())
        .limit(50)
    )
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
    streak = 0
    for position in (await session.scalars(stmt)).all():
        if position.native_returned_wei >= position.native_spent_wei:
            break
        streak += 1
    return streak


async def closed_between(
    session: AsyncSession, user_id: int, since: dt.datetime | None = None, *,
    paper: bool = False, chain: str | None = None,
) -> list[Position]:
    """Закрытые сделки пользователя. since=None — за всё время."""
    stmt = (
        select(Position)
        .where(
            Position.user_id == user_id,
            Position.status == "closed",
            Position.is_paper.is_(paper),
        )
        .order_by(Position.closed_at.asc())
    )
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
    if chain:
        stmt = stmt.where(Position.chain == chain)
    return list((await session.scalars(stmt)).all())


async def pairs_since(
    session: AsyncSession, chain: str, since: dt.datetime | None = None, limit: int = 5_000
) -> list[SeenPair]:
    """Замеченные пулы. since=None — за всё время (с ограничением по количеству)."""
    stmt = select(SeenPair).where(SeenPair.chain == chain).order_by(SeenPair.created_at.desc())
    if since is not None:
        stmt = stmt.where(SeenPair.created_at >= since)
    return list((await session.scalars(stmt.limit(limit))).all())


async def user_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def list_users(session: AsyncSession, limit: int = 50) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def set_blocked(session: AsyncSession, user_id: int, blocked: bool) -> bool:
    user = await session.get(User, user_id)
    if user is None:
        return False
    user.is_blocked = blocked
    return True


async def bad_owners(session: AsyncSession, user_id: int, chain: str, limit: int = 200) -> set[str]:
    """Владельцы контрактов, на токенах которых пользователь уже терял деньги."""
    stmt = (
        select(Position)
        .where(
            Position.user_id == user_id,
            Position.chain == chain,
            Position.status == "closed",
            Position.token_owner.is_not(None),
        )
        .order_by(Position.closed_at.desc().nullslast())
        .limit(limit)
    )
    totals: dict[str, int] = {}
    for position in (await session.scalars(stmt)).all():
        owner = (position.token_owner or "").lower()
        if not owner:
            continue
        totals[owner] = totals.get(owner, 0) + (position.native_returned_wei - position.native_spent_wei)
    return {owner for owner, pnl in totals.items() if pnl < 0}


async def update_seen_pair(session: AsyncSession, pair_id: int | None, **values) -> None:
    """Дополняет запись о пуле данными, которые стали известны после анализа."""
    if pair_id is None:
        return
    await session.execute(update(SeenPair).where(SeenPair.id == pair_id).values(**values))


async def creator_stats(
    session: AsyncSession, user_id: int, chain: str | None = None, limit: int = 300
) -> list[dict]:
    """Сводка по владельцам токенов: сколько сделок и с каким результатом."""
    stmt = (
        select(Position)
        .where(
            Position.user_id == user_id,
            Position.status == "closed",
            Position.token_owner.is_not(None),
        )
        .order_by(Position.closed_at.desc().nullslast())
        .limit(limit)
    )
    if chain:
        stmt = stmt.where(Position.chain == chain)

    buckets: dict[str, dict] = {}
    for position in (await session.scalars(stmt)).all():
        owner = (position.token_owner or "").lower()
        if not owner:
            continue
        bucket = buckets.setdefault(owner, {"owner": owner, "trades": 0, "wins": 0,
                                            "pnl": 0, "symbols": []})
        pnl = position.native_returned_wei - position.native_spent_wei
        bucket["trades"] += 1
        bucket["pnl"] += pnl
        bucket["wins"] += 1 if pnl > 0 else 0
        if position.token_symbol and position.token_symbol not in bucket["symbols"]:
            bucket["symbols"].append(position.token_symbol)
    return sorted(buckets.values(), key=lambda item: item["pnl"])


async def ab_stats(session: AsyncSession, user_id: int, chain: str,
                   since: dt.datetime | None = None) -> dict:
    """Результаты A/B-теста по группам."""
    stmt = select(Position).where(
        Position.user_id == user_id,
        Position.chain == chain,
        Position.status == "closed",
        Position.ab_group.in_(["A", "B"]),
    )
    if since is not None:
        stmt = stmt.where(Position.closed_at >= since)
    groups: dict[str, dict] = {
        "A": {"trades": 0, "wins": 0, "pnl": 0, "spent": 0},
        "B": {"trades": 0, "wins": 0, "pnl": 0, "spent": 0},
    }
    for position in (await session.scalars(stmt)).all():
        bucket = groups[position.ab_group]
        pnl = position.native_returned_wei - position.native_spent_wei
        bucket["trades"] += 1
        bucket["pnl"] += pnl
        bucket["spent"] += position.native_spent_wei
        bucket["wins"] += 1 if pnl > 0 else 0
    return groups


async def next_ab_group(session: AsyncSession, user_id: int, chain: str) -> str:
    """Чередует группы, чтобы сделок в A и B было примерно поровну."""
    stmt = (
        select(func.count())
        .select_from(Position)
        .where(Position.user_id == user_id, Position.chain == chain,
               Position.ab_group.in_(["A", "B"]))
    )
    return "B" if int(await session.scalar(stmt) or 0) % 2 else "A"


async def failed_trades(session: AsyncSession, user_id: int, since: dt.datetime,
                        kind: str = "buy") -> tuple[int, int]:
    """Сколько сделок прошло и сколько сорвалось — для советов по газу."""
    stmt = select(TradeLog).where(
        TradeLog.user_id == user_id, TradeLog.kind == kind, TradeLog.created_at >= since
    )
    rows = list((await session.scalars(stmt)).all())
    failed = sum(1 for row in rows if row.status == "failed")
    return len(rows), failed


async def recent_trades(session: AsyncSession, user_id: int | None = None,
                        limit: int = 20) -> list[TradeLog]:
    stmt = select(TradeLog).order_by(TradeLog.id.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(TradeLog.user_id == user_id)
    return list((await session.scalars(stmt)).all())


async def waiting_pairs(
    session: AsyncSession, chain: str, since: dt.datetime, limit: int = 200
) -> list[SeenPair]:
    """Пулы, где ликвидности ещё не было: их проверяем повторно."""
    stmt = (
        select(SeenPair)
        .where(
            SeenPair.chain == chain,
            SeenPair.status == "waiting",
            SeenPair.created_at >= since,
        )
        .order_by(SeenPair.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def expire_waiting_pairs(session: AsyncSession, chain: str, before: dt.datetime) -> int:
    """Снимает с ожидания пулы, куда ликвидность так и не залили."""
    result = await session.execute(
        update(SeenPair)
        .where(SeenPair.chain == chain, SeenPair.status == "waiting", SeenPair.created_at < before)
        .values(status="rejected", reason="ликвидность так и не появилась")
    )
    return int(result.rowcount or 0)


async def pair_status_counts(session: AsyncSession, chain: str,
                             since: dt.datetime | None = None) -> dict[str, int]:
    stmt = select(SeenPair.status, func.count()).where(SeenPair.chain == chain)
    if since is not None:
        stmt = stmt.where(SeenPair.created_at >= since)
    rows = (await session.execute(stmt.group_by(SeenPair.status))).all()
    return {str(status): int(count) for status, count in rows}
