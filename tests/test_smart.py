"""Кошельки, за которыми бот идёт следом.

Снайп — ставка на то, что токен окажется живым. Покупка за чужим кошельком —
ставка на чужой отбор, и она имеет смысл, только если этот отбор уже доказан.
Поэтому половина проверок здесь про то, когда повторять НЕ надо.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pytest

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import WalletTrade
from sniperbot.smart import (
    MIN_BUY_NATIVE,
    WalletBuy,
    buyer_of,
    is_win,
    score_wallets,
    trusted,
    wallet_buys,
)
from tests.test_momentum import ONE, QUOTE, TOKEN, v2_data, v3_data

POOL = "0x" + "c" * 40
WALLET = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
POOLS = {POOL: {"token": TOKEN, "quote": QUOTE, "kind": "v2"}}


def topics(wallet: str) -> list[str]:
    """Как их отдаёт нода: тема события, отправитель (роутер), получатель."""
    return ["0x" + "e" * 64, "0x" + "0" * 24 + ROUTER[2:], "0x" + "0" * 24 + wallet[2:]]


def buy_log(wallet: str, native: int = ONE, tokens: int = 1_000) -> dict:
    return {"address": POOL, "topics": topics(wallet), "data": v2_data(0, native, tokens, 0)}


# ------------------------------------------------------------ разбор логов
def test_the_buyer_is_the_recipient_not_the_router():
    """Первый адрес в логе — роутер, через него идут все. Следить надо за вторым."""
    assert buyer_of(buy_log(WALLET)) == WALLET


@pytest.mark.parametrize("entry", [
    {"topics": []},
    {"topics": ["0x" + "e" * 64]},
    {"topics": ["0x" + "e" * 64, "0x00", "0xкороткий"]},
    {},
])
def test_a_log_without_a_recipient_is_skipped(entry):
    assert buyer_of(entry) == ""


def test_only_purchases_are_recorded():
    """Продажа ничего не говорит о чутье: выходят и из удачных, и из провальных."""
    sale = {"address": POOL, "topics": topics(WALLET), "data": v2_data(1_000, 0, 0, ONE)}
    assert wallet_buys([sale], POOLS) == []


def test_dust_is_not_a_decision():
    tiny = buy_log(WALLET, native=int(MIN_BUY_NATIVE * ONE) // 10)
    assert wallet_buys([tiny], POOLS) == []


def test_one_decision_counts_once():
    """Серия свапов внутри одной транзакции — это одно решение, а не пять."""
    found = wallet_buys([buy_log(WALLET) for _ in range(5)], POOLS)
    assert len(found) == 1
    assert found[0].wallet == WALLET
    assert found[0].token == TOKEN
    assert found[0].pair == POOL
    assert found[0].price == Decimal(ONE) / Decimal(1_000)


def test_different_wallets_are_told_apart():
    other = "0x" + "9" * 40
    found = wallet_buys([buy_log(WALLET), buy_log(other)], POOLS)
    assert {item.wallet.lower() for item in found} == {WALLET.lower(), other.lower()}


def test_v3_pools_are_read_too():
    pools = {POOL: {"token": TOKEN, "quote": QUOTE, "kind": "v3"}}
    entry = {"address": POOL, "topics": topics(WALLET), "data": v3_data(-1_000, ONE)}
    found = wallet_buys([entry], pools)
    assert len(found) == 1 and found[0].wallet == WALLET


# --------------------------------------------------------------- оценка
def trade(minutes_ago: int = 60, price: str = "1", peak: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        wallet=WALLET,
        price=Decimal(price),
        peak_after=Decimal(peak) if peak else None,
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago),
    )


def test_a_fresh_buy_is_not_judged_yet():
    """Через минуту после входа ничего не известно — движение ещё не случилось."""
    score = score_wallets([trade(minutes_ago=1, peak="5")])[0]
    assert (score.decided, score.wins, score.pending) == (0, 0, 1)


def test_a_grown_token_counts_as_a_hit():
    assert is_win(trade(price="1", peak="1.3")) is True
    assert is_win(trade(price="1", peak="1.29")) is False
    assert is_win(trade(price="1")) is False, "без замеров цены вход не удачный"


def test_the_rate_is_counted_over_decided_buys_only():
    trades = [trade(peak="2") for _ in range(3)] + [trade(peak="1") for _ in range(1)]
    trades.append(trade(minutes_ago=2, peak="9"))

    score = score_wallets(trades)[0]

    assert (score.decided, score.wins, score.pending) == (4, 3, 1)
    assert score.win_rate == 75


def test_a_lucky_streak_is_not_enough():
    """Три удачи подряд выглядят как мастерство — поэтому нужен порог сделок."""
    scores = score_wallets([trade(peak="2") for _ in range(3)])
    assert trusted(scores, min_decided=5, min_win_pct=60) == set()
    assert trusted(scores, min_decided=3, min_win_pct=60) == {WALLET.lower()}


def test_a_wallet_that_misses_more_than_it_hits_is_not_followed():
    trades = [trade(peak="2") for _ in range(2)] + [trade(peak="1") for _ in range(4)]
    assert trusted(score_wallets(trades), min_decided=5, min_win_pct=60) == set()


# ----------------------------------------------------------------- база
async def test_the_same_token_is_not_counted_twice(db):
    """Докупка — продолжение прежнего решения, а не новое."""
    buy = WalletBuy(wallet=WALLET, token=TOKEN, pair=POOL, price=Decimal(1), native_wei=ONE)

    async with session_scope() as session:
        assert await repo.record_wallet_buys(session, "bsc", [buy]) == 1
    async with session_scope() as session:
        assert await repo.record_wallet_buys(session, "bsc", [buy]) == 0


async def test_peaks_are_updated_for_every_token_at_once(db):
    other_token = "0x" + "7" * 40
    async with session_scope() as session:
        await repo.record_wallet_buys(session, "bsc", [
            WalletBuy(WALLET, TOKEN, POOL, Decimal(1), ONE),
            WalletBuy(WALLET, other_token, POOL, Decimal(2), ONE),
        ])

    async with session_scope() as session:
        await repo.touch_wallet_trades(session, "bsc", {TOKEN: Decimal(3), other_token: Decimal(1)})
    async with session_scope() as session:
        await repo.touch_wallet_trades(session, "bsc", {TOKEN: Decimal(2)})

    async with session_scope() as session:
        rows = {row.token_address: row for row in await repo.wallet_trades(session, "bsc")}
    assert rows[TOKEN].peak_after == Decimal(3), "максимум не должен опускаться"
    assert rows[TOKEN].samples == 2
    # Вход был по 2, а выше 1 цена так и не поднялась — такой вход не удачный.
    assert rows[other_token].peak_after == Decimal(1)
    assert is_win(rows[other_token]) is False


async def test_old_records_are_dropped(db):
    async with session_scope() as session:
        session.add(WalletTrade(chain="bsc", wallet=WALLET, token_address=TOKEN,
                                price=Decimal(1),
                                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=30)))
    async with session_scope() as session:
        await repo.prune_wallet_trades(session, dt.datetime.now(dt.UTC) - dt.timedelta(days=14))
    async with session_scope() as session:
        assert await repo.wallet_trades(session, "bsc") == []


# ------------------------------------------- покупка следом, через охотника
async def known_wallet(chain: str = "bsc", token_prefix: str = "a", wins: int = 5) -> None:
    """Кошелёк с доказанной историей: пять входов, после которых цена росла."""
    async with session_scope() as session:
        for index in range(wins):
            session.add(WalletTrade(
                chain=chain, wallet=WALLET, token_address="0x" + token_prefix + str(index) * 39,
                price=Decimal(1), peak_after=Decimal(2), samples=5,
                created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2),
            ))


async def test_the_bot_follows_a_proven_wallet(db, monkeypatch):
    """Главный случай: адрес с историей покупает — бот покупает следом."""
    from tests.test_momentum import _hunter, _subscriber

    await _subscriber(smart_copy=True, momentum_min_trades=1000)   # разгон заведомо не сработает
    await known_wallet()
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address=POOL,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    bought: list = []
    hunter = _hunter(monkeypatch, [[buy_log(WALLET)]], bought)

    await hunter.tick()          # первый проход только ставит курсор блока
    await hunter.tick()

    assert bought == [(TOKEN, "smart")]


async def test_an_unknown_wallet_is_ignored(db, monkeypatch):
    """Без истории адрес — просто чей-то кошелёк, повторять за ним не за чем."""
    from tests.test_momentum import _hunter, _subscriber

    await _subscriber(smart_copy=True, momentum_min_trades=1000)
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address=POOL,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    bought: list = []
    hunter = _hunter(monkeypatch, [[buy_log("0x" + "8" * 40)]], bought)
    await hunter.tick()
    await hunter.tick()

    assert bought == []


async def test_following_stays_off_until_it_is_turned_on(db, monkeypatch):
    """Доверие чужому решению включает человек, а не бот за него."""
    from tests.test_momentum import _hunter, _subscriber

    await _subscriber(momentum_min_trades=1000)       # smart_copy по умолчанию выключен
    await known_wallet()
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address=POOL,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    bought: list = []
    hunter = _hunter(monkeypatch, [[buy_log(WALLET)]], bought)
    await hunter.tick()
    await hunter.tick()

    assert bought == []


async def test_watching_costs_no_extra_requests(db, monkeypatch):
    """Логи уже получены для разгона — наблюдение за кошельками бесплатное."""
    from tests.test_momentum import _hunter, _subscriber

    await _subscriber()
    async with session_scope() as session:
        await repo.add_seen_pair(session, chain="bsc", pair_address=POOL,
                                 token_address=TOKEN, router_address="0x" + "r" * 40,
                                 status="rejected")

    bought: list = []
    hunter = _hunter(monkeypatch, [[buy_log(WALLET)]], bought)
    await hunter.tick()
    await hunter.tick()

    async with session_scope() as session:
        trades = await repo.wallet_trades(session, "bsc")
    assert len(trades) == 1, "покупка запомнена даже при выключенном копировании"
    assert trades[0].wallet.lower() == WALLET.lower()
