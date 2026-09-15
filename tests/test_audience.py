"""Счётчик пользователей: что показывает и кто им управляет."""

from __future__ import annotations

import pytest

from sniperbot.audience import STATE_KEY, AudienceCounter, fmt_count, plural
from sniperbot.db import repo
from sniperbot.db.base import session_scope


# ------------------------------------------------------------------ склонение
@pytest.mark.parametrize("count,word", [
    (1, "пользователь"), (2, "пользователя"), (4, "пользователя"), (5, "пользователей"),
    (11, "пользователей"), (12, "пользователей"), (14, "пользователей"),
    (21, "пользователь"), (22, "пользователя"), (25, "пользователей"),
    (101, "пользователь"), (111, "пользователей"), (1_000, "пользователей"),
])
def test_the_word_matches_the_number(count, word):
    """«137 пользователей» читается как живой текст, «137 пользователь» — как баг."""
    assert plural(count) == word


def test_big_numbers_are_readable():
    assert fmt_count(5_400) == "5 400"
    assert fmt_count(137) == "137"


# --------------------------------------------------------------- показ строки
def test_nothing_is_shown_while_the_counter_is_off():
    """По умолчанию строки нет: включить проще, чем объясняться за раннее число."""
    assert AudienceCounter().enabled is False
    assert AudienceCounter().line(500) == ""


def test_the_line_appears_once_turned_on():
    assert AudienceCounter(enabled=True).line(137) == "👥 137 пользователей"


def test_an_empty_base_shows_nothing_even_when_on():
    """«0 пользователей» — не довод, а признак поломки."""
    assert AudienceCounter(enabled=True).line(0) == ""


# ------------------------------------------------------ решение переживает рестарт
def test_the_choice_survives_a_restart():
    saved = AudienceCounter(enabled=True).to_state()
    restored = AudienceCounter()
    restored.apply_state(saved)
    assert restored.enabled is True

    off = AudienceCounter(enabled=True)
    off.apply_state(AudienceCounter(enabled=False).to_state())
    assert off.enabled is False


@pytest.mark.parametrize("raw", ["", None, "да", "on", "2"])
def test_garbage_in_the_base_changes_nothing(raw):
    counter = AudienceCounter(enabled=True)
    counter.apply_state(raw)
    assert counter.enabled is True, "мусор не должен молча выключать показ"


async def test_the_state_key_round_trips_through_the_base(db):
    async with session_scope() as session:
        await repo.set_state(session, STATE_KEY, AudienceCounter(enabled=True).to_state())
    async with session_scope() as session:
        stored = await repo.get_state(session, STATE_KEY)

    counter = AudienceCounter()
    counter.apply_state(stored)
    assert counter.enabled is True


# ------------------------------------------------------------------- кого считаем
async def test_blocked_users_are_not_counted_on_the_screen(db):
    """Заблокированных приписывать к витрине нечестно, в статистике они остаются."""
    async with session_scope() as session:
        for user_id in (1, 2, 3):
            await repo.get_or_create_user(session, user_id, f"u{user_id}")
        await repo.set_blocked(session, 3, True)

    async with session_scope() as session:
        assert await repo.user_count(session) == 3
        assert await repo.user_count(session, include_blocked=False) == 2


# ------------------------------------------------------------ экран самой команды
async def test_the_command_shows_what_the_line_would_look_like(db, monkeypatch):
    """Владелец должен видеть будущую строку до того, как включит её людям."""
    from types import SimpleNamespace

    from aiogram.filters import CommandObject

    from sniperbot.bot.context import BotContext
    from sniperbot.bot.handlers import admin
    from sniperbot.chain.wallet import WalletService
    from sniperbot.config import Settings
    from sniperbot.security.keyvault import KeyVault

    said: list[str] = []

    async def fake_reply(message, text, markup=None, **kwargs):  # noqa: ANN001
        said.append(text)

    monkeypatch.setattr(admin, "reply", fake_reply)
    ctx = BotContext(settings=Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32),
                     registry=None, wallets=WalletService(KeyVault("k" * 32)),
                     trader=None, engine=None, notifier=None)  # type: ignore[arg-type]
    async with session_scope() as session:
        for uid in range(1, 42):
            await repo.get_or_create_user(session, uid, f"u{uid}")

    await admin.cmd_counter(SimpleNamespace(), CommandObject(args=None), ctx, is_admin=True)

    assert "выключен" in said[0]
    assert "👥 41 пользователь" in said[0], "видно, что именно появится"
    assert "мало" in said[0], "при маленькой аудитории — предупреждение"

    await admin.cmd_counter(SimpleNamespace(), CommandObject(args="on"), ctx, is_admin=True)
    assert ctx.audience.enabled is True
    async with session_scope() as session:
        assert await repo.get_state(session, STATE_KEY) == "1"
