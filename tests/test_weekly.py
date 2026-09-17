"""Недельная подстройка: бот приносит выводы сам, но ничего не меняет без спроса."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position
from sniperbot.sniper.weekly import WeeklyTuner, pack, pending_key, sent_key, unpack
from sniperbot.tune import Proposal, best_exits, current_average, exit_proposals
from sniperbot.utils.fmt import to_wei

ENTRY = Decimal("0.000001")


class Notifier:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send(self, user_id: int, text: str, **kwargs) -> None:
        self.messages.append((user_id, text))


class Registry:
    def config(self, key):  # noqa: ANN001
        from types import SimpleNamespace

        return SimpleNamespace(native_symbol="ETH", key=key, name="Robinhood Chain")


def tuner(notifier: Notifier) -> WeeklyTuner:
    from sniperbot.config import Settings

    return WeeklyTuner(Registry(), notifier, Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32))


async def trader_with(trades: int, *, peak: Decimal, result: Decimal, chain: str = "rh") -> None:
    """Пользователь с историей: сделки доходили до `peak`, закрывались на `result`."""
    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, 1, "u", default_chain=chain)
        user.active_chain = chain
        cfg = await repo.get_settings(session, 1, chain)
        cfg.take_profit_pct = 500      # заведомо далеко от лучшего
        cfg.stop_loss_pct = 70
        cfg.tp_ladder = ""
        cfg.secure_pct = 0
        for index in range(trades):
            session.add(Position(
                id=index + 1, user_id=1, chain=chain, token_address="0x" + str(index) * 40,
                token_symbol="MEME", token_decimals=18, router_address="0x" + "r" * 40,
                dex_kind="v2", status="closed", is_paper=False, amount_wei=0,
                bought_wei=to_wei(1000), native_spent_wei=to_wei("0.001"),
                native_returned_wei=to_wei(Decimal("0.001") * (1 + result / 100)),
                entry_price=ENTRY, peak_price=ENTRY * (1 + peak / 100),
                closed_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                opened_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
            ))


# ------------------------------------------------------------------ подбор
def test_the_grid_finds_the_better_pair():
    """Сделки доходили до +120%, а закрывались в минус: тейк стоит опустить."""
    trades = [(Decimal(120), Decimal(-40)) for _ in range(12)]
    average, take_profit, stop_loss, wins = best_exits(trades)
    assert take_profit <= 100
    assert average > current_average(trades)
    assert wins == 12


def test_nothing_is_proposed_on_a_handful_of_trades():
    from types import SimpleNamespace

    cfg = SimpleNamespace(take_profit_pct=500, stop_loss_pct=70, tp_ladder="")
    assert exit_proposals([(Decimal(120), Decimal(-40))] * 5, cfg) == []


def test_a_working_ladder_is_not_overwritten_by_one_number():
    """Лесенка и одиночный тейк — одна настройка: подменять её молча нельзя."""
    from types import SimpleNamespace

    cfg = SimpleNamespace(take_profit_pct=0, stop_loss_pct=30, tp_ladder="[[1.5, 40]]")
    names = {item.name for item in exit_proposals([(Decimal(120), Decimal(-40))] * 12, cfg)}
    assert "tp" not in names


def test_a_tiny_gain_is_not_worth_changing_settings():
    from types import SimpleNamespace

    # Сделки и так закрывались почти на максимуме — двигать нечего.
    cfg = SimpleNamespace(take_profit_pct=100, stop_loss_pct=50, tp_ladder="")
    assert exit_proposals([(Decimal(100), Decimal(99))] * 12, cfg) == []


# ---------------------------------------------------------------- рассылка
async def test_the_digest_arrives_with_a_button(db):
    notifier = Notifier()
    await trader_with(12, peak=Decimal(120), result=Decimal(-40))

    sent = await tuner(notifier).tick()

    assert sent == 1
    text = notifier.messages[0][1]
    assert "Подстройка по вашим сделкам" in text
    assert "/set" in text


async def test_silence_when_there_is_nothing_to_say(db):
    """Регулярное «всё хорошо» перестают читать через месяц."""
    notifier = Notifier()
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1, "u", default_chain="rh")

    assert await tuner(notifier).tick() == 0
    assert notifier.messages == []


async def test_the_letter_comes_once_a_week(db):
    notifier = Notifier()
    await trader_with(12, peak=Decimal(120), result=Decimal(-40))
    watcher = tuner(notifier)

    await watcher.tick()
    await watcher.tick()
    await watcher.tick()

    assert len(notifier.messages) == 1


async def test_the_proposal_waits_for_the_answer(db):
    """Бот ничего не меняет сам — предложение лежит и ждёт кнопки."""
    notifier = Notifier()
    await trader_with(12, peak=Decimal(120), result=Decimal(-40))
    await tuner(notifier).tick()

    async with session_scope() as session:
        pending = unpack(await repo.get_state(session, pending_key(1)))
        cfg = await repo.get_settings(session, 1, "rh")

    assert pending, "предложения должны сохраниться до нажатия"
    assert cfg.take_profit_pct == 500, "настройки не должны меняться сами"
    assert cfg.stop_loss_pct == 70


async def test_the_clock_starts_even_when_there_is_nothing_to_say(db):
    """Иначе бот пересчитывал бы одно и то же каждый час."""
    notifier = Notifier()
    async with session_scope() as session:
        await repo.get_or_create_user(session, 1, "u", default_chain="rh")

    await tuner(notifier).tick()

    async with session_scope() as session:
        assert await repo.get_state(session, sent_key(1))


def test_broken_storage_means_nothing_to_apply():
    assert unpack(None) == []
    assert unpack("не json") == []
    assert unpack('["мусор"]') == []
    assert unpack(pack([Proposal("tp", "100", "потому что")])) == [("tp", "100")]
