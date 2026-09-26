"""База под одновременной записью.

«database is locked» у бота значит не мелкое предупреждение, а возможную
потерю позиции: если запись после покупки сдалась, монеты на кошельке, а в
учёте их нет. Поэтому каждое соединение обязано ждать, а не сдаваться.
"""

from __future__ import annotations

from sqlalchemy import text


async def test_every_connection_waits_for_a_busy_database(tmp_path):
    """PRAGMA действует на одно соединение: выставить её один раз мало."""
    from sniperbot.db import base

    engine = await base.init_db(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    try:
        async with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                waited = (await connection.execute(text("PRAGMA busy_timeout"))).scalar()
                assert waited == base.SQLITE_BUSY_MS
    finally:
        await base.close_db()


async def test_a_writer_waits_for_another_instead_of_failing(tmp_path):
    """Пока один пишет, второй ждёт своей очереди и дописывает."""
    import asyncio

    from sniperbot.db import base

    engine = await base.init_db(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    try:
        async with engine.begin() as setup:
            await setup.execute(text("CREATE TABLE marks (value INTEGER)"))

        async def hold_the_lock() -> None:
            async with engine.begin() as connection:
                await connection.execute(text("INSERT INTO marks VALUES (1)"))
                await asyncio.sleep(6.0)       # дольше пяти секунд по умолчанию

        async def write_after() -> None:
            await asyncio.sleep(0.2)           # гарантированно после первого
            async with engine.begin() as connection:
                await connection.execute(text("INSERT INTO marks VALUES (2)"))

        await asyncio.gather(hold_the_lock(), write_after())

        async with engine.connect() as connection:
            count = (await connection.execute(text("SELECT COUNT(*) FROM marks"))).scalar()
        assert count == 2, "вторая запись обязана дождаться, а не потеряться"
    finally:
        await base.close_db()
