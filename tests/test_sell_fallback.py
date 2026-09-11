"""Выход из позиции, когда своя площадка перестала отвечать."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniperbot.chain.dex_adapter import PoolRef, PoolState
from sniperbot.db.models import Position
from sniperbot.sniper import executor as executor_module
from sniperbot.sniper.executor import Trader
from sniperbot.utils.fmt import to_wei

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
OWN_POOL = "0x" + "1" * 40
OTHER_POOL = "0x" + "2" * 40


class StubAdapter:
    def __init__(self, name: str, kind: str, quote: int | None) -> None:
        self.name = name
        self.kind = kind
        self._quote = quote

    async def quote_sell(self, token, amount, pool):  # noqa: ANN001
        if self._quote is None:
            raise RuntimeError("Quoter не ответил: ('execution reverted', '0x')")
        # Мелкий пул берёт только часть объёма.
        if self._quote < 0 and amount > to_wei(50):
            raise RuntimeError("execution reverted")
        return abs(self._quote)


class FakeChain:
    key = "rh"
    native_decimals = 18
    native_symbol = "ETH"

    def token_url(self, address: str) -> str:
        return f"https://explorer/{address}"


class FakeClient:
    config = FakeChain()


class FakeRegistry:
    def get(self, key):  # noqa: ANN001
        return FakeClient()

    def config(self, key):  # noqa: ANN001
        return FakeChain()


def position(**kwargs) -> Position:
    defaults = {
        "id": 157, "user_id": 1, "chain": "rh", "token_address": TOKEN,
        "token_symbol": "FLYBOOK", "token_decimals": 18, "router_address": "0x" + "r" * 40,
        "pair_address": OWN_POOL, "dex_kind": "v3", "pool_fee": 3000,
        "amount_wei": to_wei(100), "bought_wei": to_wei(100), "status": "open",
    }
    defaults.update(kwargs)
    return Position(**defaults)


def trader(monkeypatch, own: StubAdapter | None, other=None) -> Trader:  # noqa: ANN001
    instance = Trader(FakeRegistry(), None, None)  # type: ignore[arg-type]
    if own is None:
        monkeypatch.setattr(instance, "adapter_for_position",
                            lambda pos: (_ for _ in ()).throw(executor_module.TradeError("нет DEX")))
    else:
        monkeypatch.setattr(instance, "adapter_for_position", lambda pos: own)

    async def fake_find(client, token, decimals=18, route="auto"):  # noqa: ANN001
        if other is None:
            return None
        adapter, state = other
        return adapter, PoolRef(address=OTHER_POOL, kind=adapter.kind), state

    monkeypatch.setattr(executor_module, "find_best_venue", fake_find)
    return instance


async def test_own_venue_is_used_while_it_answers(monkeypatch):
    own = StubAdapter("DEX V3", "v3", to_wei(2))
    route = await trader(monkeypatch, own).sell_route(FakeClient(), position(), TOKEN, to_wei(100))
    assert route is not None
    adapter, pool, expected = route
    assert adapter is own and pool.address == OWN_POOL and expected == to_wei(2)


async def test_a_silent_venue_is_replaced_by_a_working_one(monkeypatch):
    """Главный случай: V3 не даёт котировку, а пул V2 у токена живой."""
    own = StubAdapter("DEX V3", "v3", None)
    other = StubAdapter("DEX V2", "v2", to_wei(1))
    route = await trader(monkeypatch, own, (other, None)).sell_route(
        FakeClient(), position(), TOKEN, to_wei(100))

    assert route is not None
    assert route[0] is other and route[1].address == OTHER_POOL


async def test_no_quote_anywhere_gives_up_honestly(monkeypatch):
    own = StubAdapter("DEX V3", "v3", None)
    other = StubAdapter("DEX V2", "v2", None)
    assert await trader(monkeypatch, own, (other, None)).sell_route(
        FakeClient(), position(), TOKEN, to_wei(100)) is None


async def test_a_position_without_its_own_dex_still_looks_around(monkeypatch):
    """Роутер убрали из конфига — это не повод не продавать."""
    other = StubAdapter("DEX V2", "v2", to_wei(1))
    route = await trader(monkeypatch, None, (other, None)).sell_route(
        FakeClient(), position(), TOKEN, to_wei(100))
    assert route is not None and route[0] is other


# ------------------------------------------------------------------ диагноз
async def test_partial_amount_is_offered_when_the_pool_is_thin(monkeypatch):
    thin = StubAdapter("DEX V3", "v3", -to_wei(1))      # берёт только часть
    text = await trader(monkeypatch, thin).sell_failure_text(position(), to_wei(100))
    assert "50%" in text and "мало ликвидности" in text


async def test_liquidity_without_a_quote_is_called_a_transfer_tax(monkeypatch):
    """Ликвидность есть, обмен не проходит — так выглядит налог, включённый позже."""
    own = StubAdapter("DEX V3", "v3", None)
    other = StubAdapter("DEX V2", "v2", None)
    state = PoolState(pool=PoolRef(address=OTHER_POOL, kind="v2"),
                      liquidity_native=Decimal(5), reserve_native=5 * 10**18)
    text = await trader(monkeypatch, own, (other, state)).sell_failure_text(
        position(), to_wei(100))
    assert "налог на перевод" in text


async def test_an_empty_pool_is_called_a_rug(monkeypatch):
    own = StubAdapter("DEX V3", "v3", None)
    text = await trader(monkeypatch, own).sell_failure_text(position(), to_wei(100))
    assert "rug" in text
    assert "explorer" in text          # ссылка на обозреватель, чтобы проверить самому


@pytest.mark.parametrize("quote,expected", [(to_wei(1), 50), (None, 0)])
async def test_sellable_share_reports_what_the_pool_takes(monkeypatch, quote, expected):
    adapter = StubAdapter("DEX", "v3", -quote if quote else None)
    instance = trader(monkeypatch, adapter)
    share = await instance.sellable_share(adapter, TOKEN, to_wei(100),
                                          PoolRef(address=OWN_POOL, kind="v3"))
    assert share == expected
