"""Кому открыт бот: белый список, команда /access и бан."""

from __future__ import annotations

from sniperbot.access import OPEN, PRIVATE, STATE_EXTRA, STATE_MODE, AccessPolicy, parse_ids
from sniperbot.db import repo
from sniperbot.db.base import session_scope


def test_empty_whitelist_means_the_bot_is_public():
    """Пустой ALLOWED_USER_IDS всегда означал «пускаем всех» — это и есть режим."""
    policy = AccessPolicy(admins=frozenset({1}))
    assert policy.is_open
    assert policy.allows(999)


def test_a_whitelist_closes_the_bot():
    policy = AccessPolicy(admins=frozenset({1}), env_allowed=frozenset({7}))
    assert policy.mode == PRIVATE
    assert policy.allows(7) and policy.allows(1)
    assert not policy.allows(999)


def test_command_opens_the_bot_over_the_env_file():
    """Ровно тот случай, ради которого команда и нужна: .env закрыт, открыть надо сейчас."""
    policy = AccessPolicy(admins=frozenset({1}), env_allowed=frozenset({7}))
    policy.override = OPEN
    assert policy.allows(999)


def test_command_can_close_a_bot_that_env_left_open():
    policy = AccessPolicy(admins=frozenset({1}))
    policy.override = PRIVATE
    assert not policy.allows(999)
    assert policy.allows(1)          # администратор проходит всегда


def test_admin_is_never_locked_out():
    policy = AccessPolicy(admins=frozenset({1}), env_allowed=frozenset({7}), override=PRIVATE)
    assert policy.allows(1)


def test_added_ids_join_the_whitelist():
    policy = AccessPolicy(admins=frozenset({1}), env_allowed=frozenset({7}))
    assert policy.add(42) is True
    assert policy.add(42) is False          # повторно не добавляем
    assert policy.add(7) is False           # уже есть в .env
    assert policy.allows(42)
    assert policy.allowed_ids() == [7, 42]


def test_removal_touches_only_what_the_command_added():
    """Список из .env — источник правды файла, командой его не стереть."""
    policy = AccessPolicy(env_allowed=frozenset({7}), extra={42})
    assert policy.remove(42) is True
    assert policy.remove(7) is False
    assert policy.allows(7)


def test_extra_ids_survive_a_restart():
    policy = AccessPolicy(extra={9, 3})
    assert policy.extra_value() == "3,9"
    assert parse_ids(policy.extra_value()) == {3, 9}


def test_broken_id_lists_are_ignored_not_fatal():
    assert parse_ids("1, x, 3;-4, ") == {1, 3, -4}
    assert parse_ids(None) == set()


async def test_decision_is_stored_and_read_back(db):
    """После перезапуска бот должен остаться открытым, а не закрыться снова."""
    async with session_scope() as session:
        await repo.set_state(session, STATE_MODE, OPEN)
        await repo.set_state(session, STATE_EXTRA, "42,43")

    async with session_scope() as session:
        mode = await repo.get_state(session, STATE_MODE)
        extra = await repo.get_state(session, STATE_EXTRA)

    policy = AccessPolicy(env_allowed=frozenset({7}), override=mode, extra=parse_ids(extra))
    assert policy.is_open
    assert policy.allowed_ids() == [7, 42, 43]


async def test_state_is_overwritten_not_duplicated(db):
    async with session_scope() as session:
        await repo.set_state(session, STATE_MODE, OPEN)
    async with session_scope() as session:
        await repo.set_state(session, STATE_MODE, PRIVATE)
    async with session_scope() as session:
        assert await repo.get_state(session, STATE_MODE) == PRIVATE
        assert await repo.get_state(session, "нет такого", "default") == "default"
