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


# ------------------------------------------------- включение и выключение в боте
def settings_for(**overrides) -> object:
    from sniperbot.fees import FeeSettings

    defaults = {"wallet": "0x" + "f" * 40, "deposit_bps": 200, "profit_bps": 500}
    defaults.update(overrides)
    return FeeSettings(**defaults)


def test_switch_off_keeps_the_wallet_for_switching_back_on():
    """Выключение не должно стирать адрес: включать обратно — одна команда."""
    from sniperbot.fees import apply_fee_change

    fees = settings_for()
    changed, answer = apply_fee_change(fees, "off")
    assert changed and fees.off and not fees.policy().enabled
    assert fees.wallet, "кошелёк должен сохраниться"
    assert "/fees on" in answer

    changed, _ = apply_fee_change(fees, "on")
    assert changed and fees.policy().enabled


def test_switching_on_without_a_wallet_explains_what_is_missing():
    from sniperbot.fees import apply_fee_change

    fees = settings_for(wallet="")
    changed, answer = apply_fee_change(fees, "on")
    assert not changed
    assert "wallet" in answer


def test_setting_a_wallet_turns_fees_on():
    from sniperbot.fees import apply_fee_change

    fees = settings_for(wallet="", off=True)
    changed, _ = apply_fee_change(fees, f"wallet 0x{'a' * 40}", is_address=lambda v: True)
    assert changed and fees.policy().enabled


def test_a_bad_wallet_is_refused():
    from sniperbot.fees import apply_fee_change

    fees = settings_for()
    changed, answer = apply_fee_change(fees, "wallet кошелёк", is_address=lambda v: False)
    assert not changed and "адрес" in answer
    assert fees.wallet == "0x" + "f" * 40      # старое значение не потеряно


def test_percentages_are_entered_as_percent_not_basis_points():
    from sniperbot.fees import apply_fee_change

    fees = settings_for()
    assert apply_fee_change(fees, "deposit 2.5")[0] and fees.deposit_bps == 250
    assert apply_fee_change(fees, "profit 10")[0] and fees.profit_bps == 1000
    assert apply_fee_change(fees, "refs 5")[0] and fees.referrals_needed == 5


def test_absurd_percentages_are_rejected():
    """Опечатка «/fees profit 500» не должна забрать у людей пятикратную прибыль."""
    from sniperbot.fees import apply_fee_change

    fees = settings_for()
    changed, answer = apply_fee_change(fees, "profit 500")
    assert not changed and "до 50%" in answer
    assert fees.profit_bps == 500

    assert apply_fee_change(fees, "deposit много")[0] is False


def test_unknown_command_falls_through_to_the_status_screen():
    from sniperbot.fees import apply_fee_change

    assert apply_fee_change(settings_for(), "") == (False, "")
    assert apply_fee_change(settings_for(), "непонятно") == (False, "")


def test_decision_survives_a_restart():
    from sniperbot.fees import FeeSettings, apply_fee_change

    fees = settings_for()
    apply_fee_change(fees, "deposit 3")
    apply_fee_change(fees, "off")

    restored = FeeSettings(wallet="из .env", deposit_bps=200, profit_bps=500)
    restored.apply_state(fees.to_state())
    assert restored.off is True
    assert restored.deposit_bps == 300
    assert restored.wallet == fees.wallet        # команда важнее .env


def test_garbage_in_the_state_does_not_break_startup():
    from sniperbot.fees import FeeSettings

    fees = FeeSettings(wallet="0x1", deposit_bps=200)
    fees.apply_state("мусор")
    assert fees.wallet == "0x1" and fees.deposit_bps == 200


def test_switch_stops_every_fee_including_the_entry_one():
    """«Комиссии выключены» не может означать «кроме одной»."""
    fees = settings_for(off=True)
    assert fees.policy().enabled is False
    assert fees.policy().wallet == ""        # некуда отправлять — значит не берём


# ----------------------------------------------- сколько друзей ещё нужно
def progress_for(referrals: int, *, needed: int = 3, is_admin: bool = False,
                 exempt: bool = False, wallet: str = "0x" + "f" * 40) -> str:
    from sniperbot.fees import referral_progress

    policy = FeePolicy(wallet=wallet, deposit_bps=200, profit_bps=500, referrals_needed=needed)
    return referral_progress(status_for(referrals=referrals, is_admin=is_admin,
                                        exempt=exempt, policy=policy))


def test_progress_names_the_target_and_what_is_left():
    line = progress_for(1)
    assert "1 из 3" in line
    assert "осталось 2" in line


def test_progress_shows_a_bar_of_the_right_length():
    assert "▰▱▱" in progress_for(1)
    assert "▰▰▱" in progress_for(2)
    assert "▱▱▱" in progress_for(0)


def test_long_targets_drop_the_bar_instead_of_wrapping():
    line = progress_for(2, needed=25)
    assert "2 из 25" in line and "▰" not in line


def test_finished_progress_congratulates_instead_of_counting_down():
    line = progress_for(3)
    assert "без комиссии" in line and "осталось" not in line


def test_extra_referrals_do_not_overflow_the_bar():
    assert "из 3" in progress_for(9)


def test_nothing_is_shown_when_there_is_nothing_to_earn():
    """Админам и освобождённым считать друзей незачем — комиссии и так нет."""
    assert progress_for(0, is_admin=True) == ""
    assert progress_for(0, exempt=True) == ""
    assert progress_for(0, wallet="") == ""           # комиссии выключены


def test_no_referral_programme_means_no_line():
    assert progress_for(0, needed=0) == ""


def test_the_profit_fee_is_disclosed_somewhere():
    """Списание с чужих денег должно быть названо хотя бы раз — это не косметика.

    Строку можно двигать и делать незаметнее, но не удалять: комиссия уходит с
    кошелька пользователя отдельным переводом и видна в блокчейне в любом случае.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent
              / "sniperbot" / "bot" / "handlers" / "referral.py").read_text(encoding="utf-8")
    assert "profit_pct" in source, "в /ref не осталось упоминания комиссии с прибыли"
