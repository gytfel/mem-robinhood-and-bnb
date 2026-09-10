"""Комиссии сервиса и реферальная программа: кто и сколько платит."""

from __future__ import annotations

import pytest

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.fees import (
    FeePolicy,
    deposit_exempt,
    deposit_fee,
    parse_referral,
    profit_fee,
    referral_link,
    status_for,
)

ONE = 10**18
POLICY = FeePolicy(wallet="0x" + "f" * 40, deposit_bps=200, profit_bps=500, referrals_needed=3)


# ------------------------------------------------------- комиссия за пополнение
def test_deposit_fee_is_two_percent_by_default():
    assert deposit_fee(ONE, referrals=0, is_admin=False, exempt=False, policy=POLICY) == ONE * 2 // 100


def test_three_referrals_remove_the_deposit_fee():
    """Ровно то, что обещано пользователю: пригласил троих — платить перестал."""
    assert deposit_fee(ONE, referrals=2, is_admin=False, exempt=False, policy=POLICY) > 0
    assert deposit_fee(ONE, referrals=3, is_admin=False, exempt=False, policy=POLICY) == 0
    assert deposit_fee(ONE, referrals=9, is_admin=False, exempt=False, policy=POLICY) == 0


def test_owner_never_pays():
    assert deposit_fee(ONE, referrals=0, is_admin=True, exempt=False, policy=POLICY) == 0
    assert profit_fee(ONE, 3 * ONE, is_admin=True, exempt=False, policy=POLICY) == 0


def test_manual_exemption_works_like_referrals():
    assert deposit_fee(ONE, referrals=0, is_admin=False, exempt=True, policy=POLICY) == 0
    assert profit_fee(ONE, 3 * ONE, is_admin=False, exempt=True, policy=POLICY) == 0


def test_no_wallet_means_no_fees_at_all():
    """Без кошелька сбора комиссию некуда отправлять — значит её нет."""
    off = FeePolicy(wallet="", deposit_bps=200, profit_bps=500)
    assert deposit_fee(ONE, referrals=0, is_admin=False, exempt=False, policy=off) == 0
    assert profit_fee(ONE, 5 * ONE, is_admin=False, exempt=False, policy=off) == 0


def test_fee_smaller_than_its_own_transfer_is_not_taken():
    """Иначе пользователь платит за пустую транзакцию, а сервис ничего не получает."""
    tiny = deposit_fee(10**12, referrals=0, is_admin=False, exempt=False,
                       policy=POLICY, gas_cost_wei=10**13)
    assert tiny == 0

    worth = deposit_fee(ONE, referrals=0, is_admin=False, exempt=False,
                        policy=POLICY, gas_cost_wei=10**13)
    assert worth > 0


# ---------------------------------------------------------- комиссия с прибыли
def test_profit_fee_is_five_percent_of_the_gain_not_the_amount():
    """С оборота брать нельзя: 5% от 1 монеты не то же, что 5% от заработанного."""
    fee = profit_fee(ONE, 3 * ONE, is_admin=False, exempt=False, policy=POLICY)
    assert fee == 2 * ONE * 5 // 100        # прибыль 2 монеты, комиссия с неё


def test_losing_trade_pays_nothing():
    assert profit_fee(ONE, ONE // 2, is_admin=False, exempt=False, policy=POLICY) == 0
    assert profit_fee(ONE, ONE, is_admin=False, exempt=False, policy=POLICY) == 0


# ---------------------------------------------------------------- статус и ссылки
def test_status_explains_why_the_fee_is_what_it_is():
    waiting = status_for(referrals=1, is_admin=False, exempt=False, policy=POLICY)
    assert waiting.deposit_bps == 200
    assert waiting.left_to_free == 2
    assert "1 из 3" in waiting.reason

    free = status_for(referrals=3, is_admin=False, exempt=False, policy=POLICY)
    assert free.free_deposit and free.left_to_free == 0
    assert free.profit_bps == 500          # комиссия с прибыли рефералами не снимается


def test_owner_status_says_so():
    status = status_for(referrals=0, is_admin=True, exempt=False, policy=POLICY)
    assert status.free_deposit and status.profit_bps == 0
    assert "администратор" in status.reason


def test_exempt_reason_is_named():
    _, reason = deposit_exempt(referrals=0, is_admin=False, exempt=True, policy=POLICY)
    assert "вручную" in reason


def test_referral_link_and_payload_round_trip():
    link = referral_link("@MySniperBot", 4242)
    assert link == "https://t.me/MySniperBot?start=ref4242"
    assert parse_referral("ref4242") == 4242


def test_unknown_payload_is_ignored():
    for payload in (None, "", "hello", "ref", "refabc"):
        assert parse_referral(payload) is None


def test_link_is_empty_without_a_bot_name():
    assert referral_link("", 1) == ""


# -------------------------------------------------------------------- хранение
async def test_referral_is_recorded_once_and_never_on_yourself(db):
    """Перепривязка позволила бы накрутить себе бесплатные пополнения."""
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1, "owner")
        await repo.get_or_create_user(session, 2, "friend")
        await repo.get_or_create_user(session, 3, "other")

    async with session_scope() as session:
        assert await repo.set_referrer(session, 2, 1) is True
        assert await repo.set_referrer(session, 2, 3) is False    # уже привязан
        assert await repo.set_referrer(session, 1, 1) is False    # сам себя

    async with session_scope() as session:
        assert await repo.referral_count(session, 1) == 1
        assert await repo.referral_count(session, 3) == 0


async def test_referrer_must_exist(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1, "user")
    async with session_scope() as session:
        assert await repo.set_referrer(session, 1, 999) is False


async def test_collected_fees_add_up(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1, "a")
        await repo.get_or_create_user(session, 2, "b")

    async with session_scope() as session:
        await repo.add_fee_paid(session, 1, 3 * ONE)
        await repo.add_fee_paid(session, 1, 2 * ONE)
        await repo.add_fee_paid(session, 2, ONE)

    async with session_scope() as session:
        assert await repo.fees_total(session) == 6 * ONE
        assert int((await repo.get_user(session, 1)).fees_paid_wei) == 5 * ONE


@pytest.mark.parametrize("referrals,expected", [(0, True), (2, True), (3, False), (5, False)])
async def test_free_deposit_threshold_matches_the_promise(referrals, expected):
    charged = deposit_fee(ONE, referrals=referrals, is_admin=False, exempt=False, policy=POLICY) > 0
    assert charged is expected
