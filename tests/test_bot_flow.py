"""Проверка связки «мидлварь -> пользователь -> кошелёк -> экраны бота»."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from sniperbot.access import AccessPolicy
from sniperbot.bot.context import BotContext
from sniperbot.bot.middlewares import AccessMiddleware, UserMiddleware
from sniperbot.bot.views import render_main, render_wallet
from sniperbot.chain.wallet import WalletService
from sniperbot.config import ChainConfig, RouterConfig, Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.security.keyvault import KeyVault

BALANCE = 2 * 10**18


class FakeClient:
    def __init__(self, config: ChainConfig) -> None:
        self.config = config

    async def native_balance(self, address: str) -> int:
        return BALANCE


class FakeRegistry:
    def __init__(self, configs: dict[str, ChainConfig]) -> None:
        self._configs = configs
        self._clients = {key: FakeClient(cfg) for key, cfg in configs.items()}

    @property
    def configs(self):
        return self._configs

    def get(self, key: str) -> FakeClient:
        return self._clients[key]

    def config(self, key: str) -> ChainConfig:
        return self._configs[key]


def make_chain(key: str, name: str, enabled: bool = True) -> ChainConfig:
    return ChainConfig(
        key=key, name=name, chain_id=56, native_symbol="BNB", enabled=enabled,
        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40,
        explorer_url="https://bscscan.com",
        routers=[RouterConfig("PancakeSwap V2", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)],
    )


@pytest.fixture
def ctx():
    configs = {"bsc": make_chain("bsc", "BNB Smart Chain"),
               "robinhood": make_chain("robinhood", "Robinhood Chain", enabled=False)}
    configs["robinhood"].rpc_urls = []
    settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32)
    return BotContext(
        settings=settings,
        registry=FakeRegistry(configs),  # type: ignore[arg-type]
        wallets=WalletService(KeyVault("k" * 32)),
        trader=None,  # type: ignore[arg-type]
        engine=None,  # type: ignore[arg-type]
        notifier=None,  # type: ignore[arg-type]
    )


def tg_user(uid: int = 555):
    return SimpleNamespace(id=uid, username="vlad", is_bot=False)


async def run_middleware(ctx: BotContext, uid: int = 555) -> dict:
    captured: dict = {}

    async def handler(event, data):  # noqa: ANN001
        captured.update(data)
        return "handled"

    middleware = UserMiddleware(ctx)
    result = await middleware(handler, object(), {"event_from_user": tg_user(uid)})
    assert result == "handled"
    return captured


async def test_middleware_creates_user_wallet_and_settings(db, ctx):
    data = await run_middleware(ctx)
    user = data["user"]
    assert user.wallet_address.startswith("0x") and len(user.wallet_address) == 42
    assert data["is_new_user"] is True
    assert data["chain_key"] == "bsc"
    assert data["cfg"].buy_amount == Decimal("0.01")


async def test_wallet_is_not_recreated(db, ctx):
    first = (await run_middleware(ctx))["user"].wallet_address
    second = (await run_middleware(ctx))
    assert second["user"].wallet_address == first
    assert second["is_new_user"] is False


async def test_inactive_chain_falls_back(db, ctx):
    """Если у пользователя выбрана выключенная сеть — берём рабочую."""
    await run_middleware(ctx)
    async with session_scope() as session:
        user = await repo.get_user(session, 555)
        user.active_chain = "robinhood"

    data = await run_middleware(ctx)
    assert data["chain_key"] == "bsc"


async def test_main_screen_shows_balance_and_address(db, ctx):
    data = await run_middleware(ctx)
    text = await render_main(ctx, data["user"], data["cfg"], data["chain"], open_positions=0)
    assert data["user"].wallet_address in text
    assert "2 BNB" in text
    assert "Автоснайп" in text


async def test_wallet_screen_lists_only_active_chains(db, ctx):
    data = await run_middleware(ctx)
    text = await render_wallet(ctx, data["user"], data["chain"])
    assert "BNB Smart Chain" in text
    assert "Robinhood Chain" not in text  # сеть не настроена — в балансах не показываем


async def test_access_middleware_blocks_strangers(ctx):
    calls = []

    async def handler(event, data):  # noqa: ANN001
        calls.append(1)
        return "ok"

    policy = AccessPolicy(admins=frozenset({2}), env_allowed=frozenset({1}))
    middleware = AccessMiddleware(policy)
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) is None
    assert calls == []
    assert await middleware(handler, object(), {"event_from_user": tg_user(2)}) == "ok"
    assert await middleware(handler, object(), {"event_from_user": tg_user(1)}) == "ok"
    assert len(calls) == 2


async def test_open_bot_lets_everyone_in(ctx):
    """Главное: пустой белый список — это открытый бот, а не закрытый."""
    async def handler(event, data):  # noqa: ANN001
        return "ok"

    middleware = AccessMiddleware(AccessPolicy(admins=frozenset({2})))
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) == "ok"


async def test_access_can_be_opened_without_restart(ctx):
    """/access меняет тот же объект политики, что читает мидлварь."""
    async def handler(event, data):  # noqa: ANN001
        return "ok"

    policy = AccessPolicy(admins=frozenset({2}), env_allowed=frozenset({1}))
    middleware = AccessMiddleware(policy)
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) is None

    policy.override = "open"
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) == "ok"

    policy.override = "private"
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) is None
    policy.add(999)
    assert await middleware(handler, object(), {"event_from_user": tg_user(999)}) == "ok"


async def test_banned_user_is_stopped_before_any_handler(db, ctx):
    """До этой проверки /ban был пометкой в карточке, и только."""
    from sniperbot.bot.middlewares import UserMiddleware

    await run_middleware(ctx)                       # пользователь заводится
    async with session_scope() as session:
        await repo.set_blocked(session, 555, True)

    calls = []

    async def handler(event, data):  # noqa: ANN001
        calls.append(1)
        return "handled"

    middleware = UserMiddleware(ctx)
    assert await middleware(handler, object(), {"event_from_user": tg_user(555)}) is None
    assert calls == []

    # Администратора собственный бан не запирает снаружи базы.
    assert await middleware(handler, object(),
                            {"event_from_user": tg_user(555), "is_admin": True}) == "handled"
