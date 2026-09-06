"""Инициализация асинхронного движка БД."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from sniperbot.db.models import Base

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_dir(url: str) -> None:
    marker = "sqlite+aiosqlite:///"
    if url.startswith(marker):
        path = Path(url[len(marker) :])
        if path.name and str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)


async def init_db(database_url: str) -> AsyncEngine:
    """Создаёт движок и таблицы. Вызывается один раз при старте."""
    global _engine, _session_factory
    _ensure_sqlite_dir(database_url)
    _engine = create_async_engine(database_url, echo=False, pool_pre_ping=True, future=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)

    async with _engine.begin() as conn:
        if database_url.startswith("sqlite"):
            from sqlalchemy import text

            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)
    log.info("База данных готова: %s", database_url.split("://")[0])
    return _engine


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("init_db() не был вызван")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия с автоматическим commit/rollback."""
    factory = session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
