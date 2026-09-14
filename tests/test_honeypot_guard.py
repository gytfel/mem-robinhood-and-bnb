"""Токен, из которого нельзя выйти, не должен быть куплен — и куплен повторно.

Проверка перед покупкой ловит не всё: часть ханипотов запрещает продажу именно
тем, кто покупал, а налог на продажу включают уже после того, как соберут
деньги. Поэтому проверок три: перед ручной покупкой, после входа и потом
регулярно, пока позиция открыта.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sniperbot.chain.dex_adapter import PoolRef
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.sniper.safety import (
    HoneypotSimulator,
    Rejection,
    SafetyReport,
    SimulationResult,
    first_trap,
    proven_trap,
)

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
HOLDER = "0x4D732FdDBFdBe5b5f7026bFCc831b85C4C34447d"
ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


class FakeClient:
    config = SimpleNamespace(key="bsc", wrapped_native=WNATIVE)

    def __init__(self, *, sell_passes: bool, granted: int) -> None:
        self.sell_passes = sell_passes
        self.granted = granted
        self.overrides: list[dict] = []

    async def call(self, address, abi, fn_name, *args, **kwargs):  # noqa: ANN001
        if fn_name == "allowance":
            return self.granted
        raise AssertionError(fn_name)

    async def raw_call(self, tx, overrides=None):  # noqa: ANN001
        self.overrides.append(overrides or {})
        if not self.sell_passes:
            raise RuntimeError("('execution reverted', '0x')")
        return b"\x01"


class StubAdapter:
    kind = "v2"
    name = "DEX"
    router = ROUTER
    spender = ROUTER

    def encode_sell(self, token, amount, min_out, recipient, pool):  # noqa: ANN001
        return "0xdeadbeef"


def simulator(client: FakeClient) -> HoneypotSimulator:
    return HoneypotSimulator(client, StubAdapter(), PoolRef(address="0xpool", kind="v2"))


def report_with(**kwargs) -> SafetyReport:
    token = SimpleNamespace(address=TOKEN, symbol="MEME", decimals=18)
    report = SafetyReport(token=token, chain_key="bsc", router=ROUTER)
    report.simulation = SimulationResult(**kwargs)
    return report


# ------------------------------------------ продажа глазами настоящего держателя
async def test_a_working_sell_is_confirmed():
    client = FakeClient(sell_passes=True, granted=10**30)
    ok, _ = await simulator(client).sell_works_for(TOKEN, HOLDER, 10**18)
    assert ok is True


async def test_a_reverting_sell_is_reported():
    """Главный случай: купить можно, продать нельзя."""
    client = FakeClient(sell_passes=False, granted=10**30)
    ok, detail = await simulator(client).sell_works_for(TOKEN, HOLDER, 10**18)
    assert ok is False
    assert "не проходит" in detail


async def test_an_existing_allowance_needs_no_override():
    """Разрешение уже выдано — подменять слот незачем."""
    client = FakeClient(sell_passes=True, granted=10**30)
    await simulator(client).sell_works_for(TOKEN, HOLDER, 10**18)

    overrides = client.overrides[-1]
    assert TOKEN not in overrides, "лишняя подмена состояния делает проверку менее честной"
    assert HOLDER in overrides, "монеты на газ подставить всё же надо"


async def test_missing_allowance_is_granted_in_the_simulation(monkeypatch):
    """Сразу после покупки approve ещё нет — выдаём его подменой слота."""
    client = FakeClient(sell_passes=True, granted=0)
    sim = simulator(client)
    monkeypatch.setattr(sim, "find_allowance_slot", lambda *a, **kw: _slot())

    ok, _ = await sim.sell_works_for(TOKEN, HOLDER, 10**18)

    assert ok is True
    assert TOKEN in client.overrides[-1]


async def _slot():
    return 4


async def test_unknown_when_the_allowance_slot_is_not_found(monkeypatch):
    """Не смогли выдать разрешение — значит проверить не смогли, а не поймали."""
    client = FakeClient(sell_passes=False, granted=0)
    sim = simulator(client)

    async def not_found(*args, **kwargs):
        return None

    monkeypatch.setattr(sim, "find_allowance_slot", not_found)
    ok, detail = await sim.sell_works_for(TOKEN, HOLDER, 10**18)

    assert ok is None
    assert "разрешение" in detail


async def test_nothing_to_sell_is_not_a_verdict():
    client = FakeClient(sell_passes=False, granted=0)
    ok, _ = await simulator(client).sell_works_for(TOKEN, HOLDER, 0)
    assert ok is None


# --------------------------------------------------------------- разбор вердикта
@pytest.mark.parametrize("codes,expected", [
    (["min_liquidity", "honeypot"], "honeypot"),
    (["honeypot", "sell_tax"], "honeypot"),
    (["no_simulation"], "no_simulation"),
    (["sell_tax"], "sell_tax"),
])
def test_traps_are_picked_out_of_the_verdict(codes, expected):
    denied = [Rejection(code, code) for code in codes]
    found = first_trap(denied)
    assert found is not None and found.code == expected


def test_soft_filters_are_not_traps():
    """Мало ликвидности — вопрос вкуса, а не запертых денег."""
    denied = [Rejection("min_liquidity", "мало"), Rejection("owner_share", "много у владельца")]
    assert first_trap(denied) is None


def test_proven_trap_only_on_a_failed_sell():
    assert proven_trap(report_with(available=True, can_buy=True, can_sell=False))
    assert proven_trap(report_with(available=True, can_buy=True, can_sell=True)) == ""
    assert proven_trap(report_with(available=False)) == "", "не проверили — не обвиняем"


# ------------------------------------------------------------------ чёрный список
async def test_a_remembered_honeypot_blocks_every_user(db):
    async with session_scope() as session:
        await repo.remember_honeypot(session, "bsc", TOKEN, "продажа не проходит")
    async with session_scope() as session:
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 1) is True
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 2) is True


async def test_the_owner_can_lift_the_automatic_ban(db):
    """Иначе /blacklist del молча ничего не делает — а решение должно быть за человеком."""
    async with session_scope() as session:
        await repo.remember_honeypot(session, "bsc", TOKEN, "продажа не проходит")
        await repo.add_flag(session, "bsc", TOKEN, "whitelist", 1, "снят из ЧС")
    async with session_scope() as session:
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 1) is False
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 2) is True, "снял себе, не всем"


async def test_a_clean_token_is_not_blacklisted(db):
    async with session_scope() as session:
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 1) is False


# ------------------------------------------------------- ручная покупка под замком
async def entry_blocked(monkeypatch, trap, *, user_id: int = 1) -> str:
    """Вызывает проверку ручной покупки с заранее известным вердиктом."""
    from sniperbot.bot.handlers import trade

    async def fake_trap(client, token, *, amount_native_wei, cfg):  # noqa: ANN001
        if isinstance(trap, Exception):
            raise trap
        return trap

    monkeypatch.setattr(trade, "trap_before_buy", fake_trap)

    async with session_scope() as session:
        user, _ = await repo.get_or_create_user(session, user_id, "u")
        cfg = await repo.get_settings(session, user_id, "bsc")
        session.expunge(user)
        session.expunge(cfg)

    chain = SimpleNamespace(key="bsc", native_decimals=18, name="BNB")
    ctx = SimpleNamespace(registry=SimpleNamespace(get=lambda key: FakeClient(sell_passes=True,
                                                                             granted=0)))
    from decimal import Decimal

    return await trade._entry_blocked(ctx, user, cfg, chain, TOKEN, Decimal("0.01"))


async def test_a_honeypot_is_never_bought_by_hand(db, monkeypatch):
    """Кнопка «Купить» обязана перепроверять то же, что и автоснайп."""
    blocked = await entry_blocked(monkeypatch, Rejection("honeypot", "продажа не проходит"))

    assert "honeypot" in blocked
    async with session_scope() as session:
        assert await repo.is_blacklisted(session, "bsc", TOKEN, 1) is True


async def test_an_unverifiable_token_is_not_bought_either(db, monkeypatch):
    blocked = await entry_blocked(monkeypatch, Rejection("no_simulation", "нода не умеет"))
    assert "проверить" in blocked
    assert "/set sim off" in blocked, "у человека должен остаться способ настоять"


async def test_a_clean_token_passes_the_gate(db, monkeypatch):
    assert await entry_blocked(monkeypatch, None) == ""


async def test_a_broken_check_stops_the_purchase(db, monkeypatch):
    """Не смогли проверить — значит не покупаем: вслепую тут стоит дорого."""
    blocked = await entry_blocked(monkeypatch, RuntimeError("нода отвалилась"))
    assert "Покупка отменена" in blocked


async def test_a_blacklisted_token_is_refused_before_any_check(db, monkeypatch):
    async with session_scope() as session:
        await repo.remember_honeypot(session, "bsc", TOKEN, "продажа не проходит")

    blocked = await entry_blocked(monkeypatch, None)

    assert "чёрном списке" in blocked
    assert "/blacklist del" in blocked


# ------------------------------------------------------------- цена проверки
async def test_the_allowance_slot_is_remembered(monkeypatch):
    """Иначе каждая проверка выхода заново перебирает слоты — это десятки вызовов."""
    from sniperbot.sniper import safety
    from sniperbot.utils.evm import nested_mapping_slot

    monkeypatch.setattr(safety, "_slot_cache", {}, raising=False)
    key = nested_mapping_slot(HOLDER, ROUTER, 4)

    class SlotClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(sell_passes=True, granted=0)
            self.probes = 0

        def erc20(self, address):  # noqa: ANN001
            return SimpleNamespace(encode_abi=lambda name, args: "0x")

        async def raw_call(self, tx, overrides=None):  # noqa: ANN001
            self.probes += 1
            diff = (overrides or {}).get(safety.to_checksum_address(TOKEN), {}).get("stateDiff", {})
            if key in diff:
                return safety.SLOT_PROBE_VALUE.to_bytes(32, "big")
            return b"\x00" * 32

    client = SlotClient()
    first = await simulator(client).find_allowance_slot(TOKEN, HOLDER, ROUTER)
    probes = client.probes
    second = await simulator(client).find_allowance_slot(TOKEN, HOLDER, ROUTER)

    assert first == second == 4
    assert client.probes == probes, "второй раз слот должен браться из памяти"
