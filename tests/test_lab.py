"""A/B-тест настроек, репутация создателей и разбор данных для отчётов."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, Position, SeenPair, TradeLog, utcnow
from sniperbot.settings_registry import describe_variant, parse_variant, variant_overlay
from sniperbot.utils.fmt import to_wei

OWNER_A = "0xaaa0000000000000000000000000000000000001"
OWNER_B = "0xbbb0000000000000000000000000000000000002"


def cfg(**kwargs) -> ChainSettings:
    defaults = {"user_id": 1, "chain": "bsc", "take_profit_pct": 100, "stop_loss_pct": 50,
                "buy_amount": Decimal("0.01"), "gas_mode": "normal"}
    defaults.update(kwargs)
    return ChainSettings(**defaults)


# ------------------------------------------------------------------ вариант B
def test_variant_overlay_changes_only_listed_settings():
    base = cfg()
    overlay = variant_overlay(base, {"tp": "300", "sl": "30"})

    assert overlay.take_profit_pct == 300
    assert overlay.stop_loss_pct == 30
    assert overlay.buy_amount == base.buy_amount        # остальное берётся из основных
    assert base.take_profit_pct == 100                  # оригинал не тронут


def test_variant_ignores_unknown_and_broken_values():
    overlay = variant_overlay(cfg(), {"нетакой": "1", "tp": "не число", "sl": "40"})
    assert overlay.stop_loss_pct == 40
    assert overlay.take_profit_pct == 100


def test_variant_ignores_user_scoped_settings():
    """Тестовый режим — свойство пользователя, в A/B его подменять нельзя."""
    overlay = variant_overlay(cfg(), {"dry": "on"})
    assert overlay is not None
    assert not hasattr(overlay, "_overrides") or "dry_run" not in getattr(overlay, "_overrides", {})


def test_empty_variant_returns_original_object():
    base = cfg()
    assert variant_overlay(base, {}) is base


def test_parse_variant_survives_garbage():
    assert parse_variant('{"tp": 300}') == {"tp": 300}
    assert parse_variant("не json") == {}
    assert parse_variant("[1,2]") == {}
    assert parse_variant(None) == {}
    assert parse_variant("") == {}


def test_describe_variant_uses_human_titles():
    assert "Тейк-профит = 300" in describe_variant({"tp": 300})
    assert describe_variant({}) == "пусто"


# ------------------------------------------------------------- группы и итоги
async def test_ab_groups_alternate(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        assert await repo.next_ab_group(session, 1, "bsc") == "A"
        session.add(Position(user_id=1, chain="bsc", token_address="0x1", router_address="0x1",
                             status="open", ab_group="A"))

    async with session_scope() as session:
        assert await repo.next_ab_group(session, 1, "bsc") == "B"
        session.add(Position(user_id=1, chain="bsc", token_address="0x2", router_address="0x1",
                             status="open", ab_group="B"))

    async with session_scope() as session:
        assert await repo.next_ab_group(session, 1, "bsc") == "A"


async def test_ab_stats_split_results_by_group(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        # группа A: одна прибыльная, группа B: одна убыточная
        session.add(Position(user_id=1, chain="bsc", token_address="0x1", router_address="0x1",
                             status="closed", ab_group="A", native_spent_wei=to_wei("0.1"),
                             native_returned_wei=to_wei("0.3"), closed_at=utcnow()))
        session.add(Position(user_id=1, chain="bsc", token_address="0x2", router_address="0x1",
                             status="closed", ab_group="B", native_spent_wei=to_wei("0.1"),
                             native_returned_wei=to_wei("0.05"), closed_at=utcnow()))
        # сделка вне теста в статистику не попадает
        session.add(Position(user_id=1, chain="bsc", token_address="0x3", router_address="0x1",
                             status="closed", native_spent_wei=to_wei("1"),
                             native_returned_wei=to_wei("5"), closed_at=utcnow()))

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    async with session_scope() as session:
        stats = await repo.ab_stats(session, 1, "bsc", since)

    assert stats["A"]["trades"] == 1 and stats["A"]["wins"] == 1
    assert stats["B"]["trades"] == 1 and stats["B"]["wins"] == 0
    assert stats["A"]["pnl"] > 0 > stats["B"]["pnl"]


# ------------------------------------------------------------ репутация авторов
async def test_creator_stats_are_sorted_worst_first(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        session.add(Position(user_id=1, chain="bsc", token_address="0x1", router_address="0x1",
                             status="closed", token_owner=OWNER_A, token_symbol="GOOD",
                             native_spent_wei=to_wei("0.1"), native_returned_wei=to_wei("0.5"),
                             closed_at=utcnow()))
        session.add(Position(user_id=1, chain="bsc", token_address="0x2", router_address="0x1",
                             status="closed", token_owner=OWNER_B, token_symbol="RUG",
                             native_spent_wei=to_wei("0.2"), native_returned_wei=0,
                             closed_at=utcnow()))

    async with session_scope() as session:
        stats = await repo.creator_stats(session, 1, "bsc")

    assert [item["owner"] for item in stats] == [OWNER_B.lower(), OWNER_A.lower()]
    assert stats[0]["symbols"] == ["RUG"]
    assert stats[1]["wins"] == 1


# ------------------------------------------------------------- данные отчётов
async def test_seen_pair_details_are_updated(db):
    async with session_scope() as session:
        pair = await repo.add_seen_pair(session, chain="bsc", pair_address="0xPAIR",
                                        token_address="0xTOKEN", block_number=10)
        pair_id = pair.id

    async with session_scope() as session:
        await repo.update_seen_pair(session, pair_id, token_symbol="PEPE",
                                    token_name="Pepe Coin", first_block_swaps=7)

    async with session_scope() as session:
        stored = await session.get(SeenPair, pair_id)
    assert stored.token_symbol == "PEPE"
    assert stored.first_block_swaps == 7


async def test_update_seen_pair_without_id_is_safe(db):
    async with session_scope() as session:
        await repo.update_seen_pair(session, None, token_symbol="X")


async def test_failed_trades_counted_for_gas_advice(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        for status in ("success", "failed", "failed"):
            session.add(TradeLog(user_id=1, chain="bsc", kind="buy", status=status,
                                 created_at=utcnow()))
        session.add(TradeLog(user_id=1, chain="bsc", kind="sell", status="failed",
                             created_at=utcnow()))

    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    async with session_scope() as session:
        total, failed = await repo.failed_trades(session, 1, since, kind="buy")

    assert total == 3          # продажа в подсчёт покупок не попала
    assert failed == 2


async def test_recent_trades_are_newest_first(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        for token in ("0x1", "0x2", "0x3"):
            session.add(TradeLog(user_id=1, chain="bsc", kind="buy", status="success",
                                 token_address=token, created_at=utcnow()))

    async with session_scope() as session:
        trades = await repo.recent_trades(session, limit=2)

    assert len(trades) == 2
    assert trades[0].token_address == "0x3"


@pytest.mark.parametrize("payload", ['{"tp": 300, "sl": 30}', '{"gasmode": "turbo"}'])
def test_variant_json_roundtrip(payload):
    variant = parse_variant(payload)
    assert json.loads(json.dumps(variant)) == variant
    assert variant_overlay(cfg(), variant) is not None
