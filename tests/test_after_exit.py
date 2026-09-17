"""Что было с токеном после выхода — материал для настройки тейка.

Сама сделка не отвечает на вопрос «рано или вовремя я продал»: она закончилась
на той цене, на которой закончилась. Ответ даёт только то, что случилось
дальше, и собрать его можно лишь заранее.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position
from sniperbot.reports import (
    MIN_AFTER_SAMPLE,
    after_rows,
    render_after,
    to_rows,
    trades_csv,
)
from sniperbot.utils.fmt import to_wei

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
ENTRY = Decimal("0.000001")


def closed(**kwargs) -> Position:
    defaults = {
        "id": 1, "user_id": 1, "chain": "rh", "token_address": TOKEN, "token_symbol": "MEME",
        "token_decimals": 18, "router_address": "0x" + "r" * 40, "status": "closed",
        "dex_kind": "v2", "pool_fee": 0, "source": "auto", "is_paper": False, "ab_group": "",
        "amount_wei": 0, "bought_wei": to_wei(1000),
        "native_spent_wei": to_wei("0.001"), "native_returned_wei": to_wei("0.0015"),
        "entry_price": ENTRY, "last_price": ENTRY * 2, "after_samples": 5,
        "closed_at": dt.datetime.now(dt.UTC),
    }
    defaults.update(kwargs)
    return Position(**defaults)


def report_of(positions: list[Position]) -> str:
    return render_after(to_rows(positions), "ETH")


# ------------------------------------------------------------- сбор данных
async def test_a_just_closed_trade_is_followed(db):
    """Сделку, которая только что закрылась, надо ещё подержать в поле зрения."""
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(closed(id=None))

    async with session_scope() as session:
        found = await repo.recently_closed(session, dt.timedelta(minutes=60))

    assert len(found) == 1


async def test_an_old_trade_is_left_alone(db):
    """Через час наблюдение бессмысленно: движение мемкоина давно закончилось."""
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(closed(id=None, closed_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=3)))

    async with session_scope() as session:
        assert await repo.recently_closed(session, dt.timedelta(minutes=60)) == []


async def test_a_written_off_position_is_not_followed(db):
    """Из неё нечего было продавать — цена после «выхода» ничего не объясняет."""
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(closed(id=None, native_returned_wei=0, exit_reason="stuck"))

    async with session_scope() as session:
        assert await repo.recently_closed(session, dt.timedelta(minutes=60)) == []


async def test_the_highest_and_the_lowest_are_remembered(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        position = closed(id=None, after_samples=0)
        session.add(position)
        await session.flush()
        position_id = position.id

    for price in ("0.000003", "0.000005", "0.0000004", "0.000002"):
        async with session_scope() as session:
            await repo.track_after_exit(session, position_id, Decimal(price))

    async with session_scope() as session:
        stored = await session.get(Position, position_id)
        assert stored.after_peak_price == Decimal("0.000005")
        assert stored.after_low_price == Decimal("0.0000004")
        assert stored.after_samples == 4


async def test_a_broken_quote_changes_nothing(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        position = closed(id=None, after_samples=0)
        session.add(position)
        await session.flush()
        position_id = position.id

    async with session_scope() as session:
        await repo.track_after_exit(session, position_id, Decimal(0))

    async with session_scope() as session:
        stored = await session.get(Position, position_id)
        assert stored.after_peak_price is None and stored.after_samples == 0


# ----------------------------------------------------------------- выводы
def test_nothing_is_claimed_on_a_handful_of_trades():
    """Девять сделок — это совпадение, а не наблюдение."""
    few = [closed(id=i, after_peak_price=ENTRY * 10) for i in range(MIN_AFTER_SAMPLE - 1)]
    assert report_of(few) == ""


def test_an_early_exit_is_called_out():
    """Токен уходил вверх после каждой продажи — значит тейк слишком тесный."""
    rows = [closed(id=i, after_peak_price=ENTRY * 4, after_low_price=ENTRY * 2)
            for i in range(12)]

    text = report_of(rows)

    assert "выходите рано" in text
    assert "+100%" in text, "медиана упущенного роста считается от цены выхода"
    assert "/optimize" in text


def test_a_timely_exit_is_confirmed():
    """После продажи всё падало — тянуть дольше значило бы отдавать прибыль."""
    rows = [closed(id=i, after_peak_price=ENTRY * 2, after_low_price=ENTRY / 2)
            for i in range(12)]

    text = report_of(rows)

    assert "выходите вовремя" in text
    assert "-75%" in text


def test_trades_without_watching_are_not_counted():
    rows = [closed(id=i, after_samples=0) for i in range(20)]
    assert after_rows(to_rows(rows)) == []
    assert report_of(rows) == ""


def test_the_file_carries_the_numbers_too():
    """Чтобы можно было посчитать своё в таблице, а не верить выводу бота."""
    csv_text = trades_csv(to_rows([closed(after_peak_price=ENTRY * 3,
                                          after_low_price=ENTRY)]))

    assert "после выхода макс_%" in csv_text
    assert "50,00" in csv_text      # ×3 от входа против выхода ×2 — это +50%
    assert "-50,00" in csv_text


def test_the_verdict_weighs_the_move_not_the_count():
    """Разбор, на котором вывод ошибался: просадка после каждой сделки, но мелкая.

    Токен уходил вверх на 100% от цены выхода и проседал на 33%. По числу
    случаев «обвалов» больше, по деньгам — рост вдвое больше просадки, и
    держать дольше стоило.
    """
    rows = [closed(id=i,
                   last_price=ENTRY * Decimal("1.8"),
                   after_peak_price=ENTRY * (Decimal("3.6") if i % 3 else Decimal("1.9")),
                   after_low_price=ENTRY * Decimal("1.2"))
            for i in range(14)]

    text = report_of(rows)

    assert "выходите рано" in text, "ход вверх был вдвое больше хода вниз"
