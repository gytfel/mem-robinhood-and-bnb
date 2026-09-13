"""Проверки получателя перед выводом: чужая сеть, опечатка, незнакомый адрес."""

from __future__ import annotations

import pytest

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.withdrawals import Destination, address_problem, destination_warning

GOOD = "0x061AE1c324608Be3eC56a8d397F1e9EA95989E8f"


# ------------------------------------------------------------- чужие сети
@pytest.mark.parametrize("address,chain", [
    ("bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", "Bitcoin"),
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "Bitcoin"),
    ("TJRabPrwbZy45sbavfcjinPJC18kjpRTv8", "Tron"),
    ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdM1qUM6uGBgL", "Solana"),
    ("cosmos1qypqxpq9qcrsszg2pvxq6rs0zqg3yyc5lzv7xu", "Cosmos"),
    ("rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH", "XRP"),
])
def test_addresses_of_other_blockchains_are_refused(address, chain):
    """Отправленное в чужую сеть не дойдёт и не вернётся."""
    problem = address_problem(address)
    assert chain in problem


def test_a_typo_in_a_checksummed_address_is_caught():
    """Заглавные буквы в адресе — это контрольная сумма; она и ловит опечатку."""
    broken = GOOD[:-3] + ("E8F" if GOOD[-3:] != "E8F" else "e8f")
    assert "контрольная сумма" in address_problem(broken)


def test_a_correct_address_passes_in_both_forms():
    assert address_problem(GOOD) == ""
    assert address_problem(GOOD.lower()) == ""      # без заглавных сверять нечего


def test_nonsense_and_emptiness_are_named():
    assert "не похоже на адрес" in address_problem("мой кошелёк")
    assert "Пришлите адрес" in address_problem("")
    assert "не похоже на адрес" in address_problem("0x1234")


def test_burn_address_is_refused():
    assert "сжигания" in address_problem("0x" + "0" * 40)
    assert "сжигания" in address_problem("0x000000000000000000000000000000000000dEaD")


# ------------------------------------------------------ предупреждение о сети
def test_an_address_new_to_this_chain_raises_a_question():
    """Так выглядит и биржевой депозит, и кошелёк без добавленной сети."""
    warning = destination_warning(Destination(), "Robinhood Chain")
    assert "Robinhood Chain" in warning
    assert "биржа" in warning.lower()


def test_a_contract_is_called_a_contract():
    warning = destination_warning(Destination(has_code=True), "Robinhood Chain")
    assert "контракт" in warning


def test_a_used_address_passes_without_questions():
    """Вопрос, который задают каждый раз, перестают читать."""
    assert destination_warning(Destination(nonce=3), "Robinhood Chain") == ""
    assert destination_warning(Destination(balance=10**18), "Robinhood Chain") == ""


# ------------------------------------------------------------- память адресов
async def test_a_repeated_address_is_remembered(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        assert await repo.withdrawn_before(session, 1, "rh", GOOD) is False

    async with session_scope() as session:
        await repo.log_wallet_event(session, user_id=1, chain="rh", kind="withdraw",
                                    amount_wei=10**15, tx_hash="0xabc", address=GOOD)

    async with session_scope() as session:
        assert await repo.withdrawn_before(session, 1, "rh", GOOD) is True
        assert await repo.withdrawn_before(session, 1, "rh", GOOD.lower()) is True
        assert await repo.withdrawn_before(session, 1, "bsc", GOOD) is False   # другая сеть
        assert await repo.withdrawn_before(session, 2, "rh", GOOD) is False    # другой человек


async def test_a_deposit_does_not_count_as_a_known_address(db):
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1)
        await repo.log_wallet_event(session, user_id=1, chain="rh", kind="deposit",
                                    amount_wei=10**15, address=GOOD)
    async with session_scope() as session:
        assert await repo.withdrawn_before(session, 1, "rh", GOOD) is False
