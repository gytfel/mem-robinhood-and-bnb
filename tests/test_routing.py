"""Маршрутизация сообщений: команда важнее незавершённого ввода.

Проверка идёт через настоящий Dispatcher: фильтры и порядок роутеров — это
ровно то, что не видно в юнит-тестах и что ломается молча.
"""

from __future__ import annotations

import datetime as dt

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Update
from aiogram.types import Message as TgMessage
from aiogram.types import User as TgUser

from sniperbot.access import AccessPolicy
from sniperbot.bot.context import BotContext
from sniperbot.bot.handlers import build_router
from sniperbot.bot.handlers.wallet import WithdrawStates
from sniperbot.bot.middlewares import (
    AccessMiddleware,
    CommandEscapeMiddleware,
    UserMiddleware,
)
from sniperbot.chain.wallet import WalletService
from sniperbot.config import ChainConfig, RouterConfig, Settings
from sniperbot.security.keyvault import KeyVault

USER_ID = 555
CHAT_ID = 555


class RecordingSession(BaseSession):
    """Ничего не отправляет, но помнит, что бот собирался сказать."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):  # noqa: ANN001
        text = getattr(method, "text", None)
        if text:
            self.sent.append(text)
        return TgMessage(
            message_id=1, date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"), text=text or "",
        ).as_(bot)

    async def stream_content(self, *args, **kwargs):  # noqa: ANN002, ANN003
        yield b""


class FakeClient:
    def __init__(self, config: ChainConfig) -> None:
        self.config = config

    async def native_balance(self, address: str) -> int:
        return 10**18


class FakeRegistry:
    def __init__(self, configs) -> None:  # noqa: ANN001
        self.configs = configs
        self._clients = {key: FakeClient(cfg) for key, cfg in configs.items()}

    def get(self, key):  # noqa: ANN001
        return self._clients[key]

    def config(self, key):  # noqa: ANN001
        return self.configs[key]


# Роутеры модульные и к двум диспетчерам не подключаются: собираем связку один
# раз на весь файл, а состояние чистим перед каждым тестом.
_BUNDLE: tuple | None = None


def _build() -> tuple:
    chain = ChainConfig(
        key="rh", name="Robinhood Chain", chain_id=1, native_symbol="ETH", enabled=True,
        rpc_urls=["http://localhost"], wrapped_native="0x" + "b" * 40,
        routers=[RouterConfig("DEX", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)],
    )
    ctx = BotContext(
        settings=Settings(BOT_TOKEN="123:AA", MASTER_KEY="k" * 32),
        registry=FakeRegistry({"rh": chain}),  # type: ignore[arg-type]
        wallets=WalletService(KeyVault("k" * 32)),
        trader=None, engine=None, notifier=None,  # type: ignore[arg-type]
    )
    session = RecordingSession()
    bot = Bot(token="123:AAHf-test-token-value-000000000000000", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp.message.outer_middleware(CommandEscapeMiddleware())
    for observer in (dp.message, dp.callback_query):
        observer.middleware(AccessMiddleware(AccessPolicy()))
        observer.middleware(UserMiddleware(ctx))
    dp.include_router(build_router())
    return bot, dp, session


@pytest.fixture
async def bot_and_dispatcher(db):
    global _BUNDLE
    if _BUNDLE is None:
        _BUNDLE = _build()
    bot, dp, session = _BUNDLE
    session.sent.clear()
    await dp.storage.close()
    await FSMContext(storage=dp.storage,
                     key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=USER_ID)).clear()
    return bot, dp, session


def update(text: str) -> Update:
    return Update(
        update_id=1,
        message=TgMessage(
            message_id=2, date=dt.datetime.now(dt.UTC),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=TgUser(id=USER_ID, is_bot=False, first_name="Vlad"),
            text=text,
        ),
    )


async def state_of(bot: Bot, dp: Dispatcher) -> FSMContext:
    return FSMContext(storage=dp.storage, key=StorageKey(bot_id=bot.id, chat_id=CHAT_ID,
                                                         user_id=USER_ID))


async def test_command_works_while_the_bot_waits_for_an_amount(bot_and_dispatcher):
    """Главная жалоба: /positions в режиме ввода отвечал «не понял сумму»."""
    bot, dp, session = bot_and_dispatcher
    state = await state_of(bot, dp)
    await state.set_state(WithdrawStates.amount)
    await state.update_data(address="0x" + "a" * 40)

    await dp.feed_update(bot, update("/positions"))

    answer = "\n".join(session.sent)
    assert "Не понял сумму" not in answer
    assert "позиций" in answer.lower(), "должен ответить экран позиций"
    assert await state.get_state() is None, "режим ввода должен сброситься"


async def test_plain_text_still_reaches_the_input_step(bot_and_dispatcher):
    """Выбивать должна команда, а не любое сообщение: ввод обязан работать."""
    bot, dp, session = bot_and_dispatcher
    state = await state_of(bot, dp)
    await state.set_state(WithdrawStates.amount)
    await state.update_data(address="0x" + "a" * 40)

    await dp.feed_update(bot, update("не число"))

    assert "Не понял сумму" in "\n".join(session.sent)
    assert await state.get_state() == WithdrawStates.amount.state


async def test_a_command_outside_any_input_mode_works_as_before(bot_and_dispatcher):
    bot, dp, session = bot_and_dispatcher
    await dp.feed_update(bot, update("/help"))
    assert session.sent, "команда вне режима ввода тоже должна отвечать"
