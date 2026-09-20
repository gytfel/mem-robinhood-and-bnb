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
from sniperbot.sniper.executor import Trader

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
        name = type(method).__name__
        if name == "GetMe":
            return TgUser(id=1, is_bot=True, first_name="Sniper", username="sniper_bot")
        if name in {"SendDocument", "SendPhoto"}:
            self.sent.append(getattr(method, "caption", "") or "<файл>")
            name = "SendMessage"
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
    """Узел без сети: отвечает на то, что спрашивают экраны бота."""

    requests = 0
    failures = 0
    rpc_url = "http://localhost"

    def __init__(self, config: ChainConfig) -> None:
        self.config = config

    async def native_balance(self, address: str) -> int:
        return 10**18

    async def block_number(self) -> int:
        return 1_000_000

    async def gas_fees(self, multiplier: float = 1.0, priority: float = 1.0) -> dict:
        return {"gasPrice": 10**9}

    async def transaction_count(self, address: str, block: str = "pending") -> int:
        return 0

    async def call(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("сети нет — экран обязан пережить это сам")

    async def raw_call(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("сети нет — экран обязан пережить это сам")


class StubHunter:
    trending: dict = {}
    watched: dict = {}
    last_tick: dict = {}


class StubEngine:
    running = True
    scanners: dict = {}
    hunter = StubHunter()

    def status(self) -> dict:
        return {}


class StubNotifier:
    async def send(self, user_id: int, text: str, **kwargs) -> None:
        return None


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
    settings = Settings(BOT_TOKEN="123:AA", MASTER_KEY="k" * 32, ADMIN_IDS=str(USER_ID))
    registry = FakeRegistry({"rh": chain})
    wallets = WalletService(KeyVault("k" * 32))
    ctx = BotContext(
        settings=settings,
        registry=registry,  # type: ignore[arg-type]
        wallets=wallets,
        trader=Trader(registry, wallets, settings),  # type: ignore[arg-type]
        engine=StubEngine(),  # type: ignore[arg-type]
        notifier=StubNotifier(),  # type: ignore[arg-type]
        access=AccessPolicy(admins=frozenset({USER_ID})),
    )
    session = RecordingSession()
    bot = Bot(token="123:AAHf-test-token-value-000000000000000", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp.message.outer_middleware(CommandEscapeMiddleware())
    for observer in (dp.message, dp.callback_query):
        observer.middleware(AccessMiddleware(ctx.access))
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


# ------------------------------------------------------ боевая проверка меню
def _walk(router):
    yield router
    for child in router.sub_routers:
        yield from child.sub_routers or []
        yield child


def _message_handlers(dispatcher: Dispatcher) -> list:
    seen, found = set(), []
    stack = [dispatcher]
    while stack:
        router = stack.pop()
        if id(router) in seen:
            continue
        seen.add(id(router))
        found.extend(router.message.handlers)
        stack.extend(router.sub_routers)
    return found


def _commands_of(handler) -> set[str]:  # noqa: ANN001
    from aiogram.filters import Command

    names = set()
    for flt in handler.filters or []:
        callback = getattr(flt, "callback", flt)
        if isinstance(callback, Command):
            names.update(getattr(item, "pattern", str(item)) for item in callback.commands)
    return names


async def test_every_menu_command_has_exactly_one_handler(bot_and_dispatcher):
    """Команда без обработчика молчит, с двумя — отвечает дважды. Видно только вживую."""
    from sniperbot.bot.app import ADMIN_COMMANDS, COMMANDS

    _, dp, _ = bot_and_dispatcher
    owners: dict[str, list[str]] = {}
    for handler in _message_handlers(dp):
        for command in _commands_of(handler):
            owners.setdefault(command, []).append(handler.callback.__name__)

    wrong = {command.command: owners.get(command.command, [])
             for command in COMMANDS + ADMIN_COMMANDS
             if len(owners.get(command.command, [])) != 1}
    assert not wrong, f"команды с неверным числом обработчиков: {wrong}"


async def test_every_handler_gets_what_it_asks_for(bot_and_dispatcher):
    """Параметр, который некому подставить, роняет команду при первом же вызове."""
    import inspect

    provided = {
        "message", "event", "update", "bot", "event_from_user", "event_chat", "state",
        "raw_state", "fsm_storage", "event_update", "event_router", "handler", "dispatcher",
        "command", "callback_query", "callback_data", "ctx", "user", "cfg", "chain",
        "chain_key", "is_new_user", "is_admin", "event_context", "business_connection_id",
    }
    _, dp, _ = bot_and_dispatcher

    missing = []
    for handler in _message_handlers(dp):
        for param in inspect.signature(handler.callback).parameters.values():
            if param.default is not inspect.Parameter.empty:
                continue
            if param.kind in {param.VAR_POSITIONAL, param.VAR_KEYWORD}:
                continue
            if param.name not in provided:
                missing.append(f"{handler.callback.__name__}({param.name})")
    assert not missing, f"некому подставить: {missing}"


async def test_every_menu_command_answers_something(bot_and_dispatcher):
    """Каждая команда меню обязана ответить: молчание пользователь читает как поломку."""
    from sniperbot.bot.app import ADMIN_COMMANDS, COMMANDS

    bot, dp, session = bot_and_dispatcher
    silent = []
    for command in COMMANDS + ADMIN_COMMANDS:
        session.sent.clear()
        await dp.feed_update(bot, update(f"/{command.command}"))
        if not session.sent:
            silent.append("/" + command.command)
    assert not silent, f"промолчали: {silent}"


async def test_a_honeypot_is_refused_on_the_way_to_the_trader(bot_and_dispatcher):
    """Сквозь настоящий роутинг: /buy обязан упереться в проверку выхода."""
    from sniperbot.bot.handlers import trade as trade_handlers
    from sniperbot.sniper.safety import Rejection

    bot, dp, session = bot_and_dispatcher
    token = "0x55d398326f99059fF775485246999027B3197955"
    bought = []

    async def refuse(client, token, *, amount_native_wei, cfg):  # noqa: ANN001
        return Rejection("honeypot", "продажа не проходит в симуляции")

    async def buy(*args, **kwargs):  # noqa: ANN002, ANN003
        bought.append(args)

    ctx = dp["ctx"]
    original_trap, original_buy = trade_handlers.trap_before_buy, ctx.trader.buy
    trade_handlers.trap_before_buy, ctx.trader.buy = refuse, buy
    try:
        await dp.feed_update(bot, update(f"/buy {token} 0.05"))
        answer = "\n".join(session.sent)

        assert bought == [], "деньги ушли в ханипот"
        assert "honeypot" in answer
        # Второй заход отсекается ещё раньше — токен уже в чёрном списке.
        trade_handlers.trap_before_buy = original_trap
        session.sent.clear()
        await dp.feed_update(bot, update(f"/buy {token} 0.05"))
        assert "чёрном списке" in "\n".join(session.sent)
    finally:
        trade_handlers.trap_before_buy, ctx.trader.buy = original_trap, original_buy


async def test_the_owner_turns_the_user_counter_on_and_off(bot_and_dispatcher):
    """Сквозь настоящий роутинг: команда меняет и экран, и запись в базе."""
    from sniperbot.audience import STATE_KEY
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope

    bot, dp, session = bot_and_dispatcher
    ctx = dp["ctx"]
    try:
        await dp.feed_update(bot, update("/counter on"))
        assert "Счётчик включён" in "\n".join(session.sent)
        assert ctx.audience.enabled is True
        async with session_scope() as db_session:
            assert await repo.get_state(db_session, STATE_KEY) == "1"

        session.sent.clear()
        await dp.feed_update(bot, update("/start"))
        assert "пользовател" in "\n".join(session.sent), "число должно появиться на старте"

        session.sent.clear()
        await dp.feed_update(bot, update("/counter off"))
        assert ctx.audience.enabled is False
        async with session_scope() as db_session:
            assert await repo.get_state(db_session, STATE_KEY) == "0"

        session.sent.clear()
        await dp.feed_update(bot, update("/start"))
        assert "пользовател" not in "\n".join(session.sent)
    finally:
        ctx.audience.enabled = False


async def test_a_stranger_cannot_touch_the_counter(bot_and_dispatcher):
    """Команда админская: чужой её не переключит."""
    import datetime as dt

    from aiogram.types import Chat, Update
    from aiogram.types import Message as TgMessage
    from aiogram.types import User as TgUser

    bot, dp, session = bot_and_dispatcher
    ctx = dp["ctx"]
    stranger = Update(update_id=2, message=TgMessage(
        message_id=3, date=dt.datetime.now(dt.UTC),
        chat=Chat(id=999, type="private"),
        from_user=TgUser(id=999, is_bot=False, first_name="Чужой"),
        text="/counter on"))

    await dp.feed_update(bot, stranger)

    assert ctx.audience.enabled is False
    assert "только для администратора" in "\n".join(session.sent)


async def test_the_weekly_proposal_is_applied_only_by_the_button(bot_and_dispatcher):
    """Сквозь настоящий роутинг: пока кнопку не нажали, настройки прежние."""
    import datetime as dt

    from aiogram.types import CallbackQuery

    from sniperbot.bot.keyboards import TuneCB
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope
    from sniperbot.sniper.weekly import pending_key

    bot, dp, session = bot_and_dispatcher
    await dp.feed_update(bot, update("/start"))          # пользователь заведён

    async with session_scope() as db_session:
        await repo.set_state(db_session, pending_key(USER_ID), '[["sl", "45"]]')
        before = await repo.get_settings(db_session, USER_ID, "rh")
        assert before.stop_loss_pct != 45

    letter = TgMessage(message_id=7, date=dt.datetime.now(dt.UTC),
                       chat=Chat(id=CHAT_ID, type="private"), text="письмо с предложениями")
    session.sent.clear()
    await dp.feed_update(bot, Update(update_id=9, callback_query=CallbackQuery(
        id="1", chat_instance="c", data=TuneCB(action="apply").pack(),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="Vlad"),
        message=letter,
    )))

    async with session_scope() as db_session:
        after = await repo.get_settings(db_session, USER_ID, "rh")
        left = await repo.get_state(db_session, pending_key(USER_ID))

    assert after.stop_loss_pct == 45
    assert not left, "применённое предложение не должно остаться висеть"


async def test_declining_the_proposal_changes_nothing(bot_and_dispatcher):
    import datetime as dt

    from aiogram.types import CallbackQuery

    from sniperbot.bot.keyboards import TuneCB
    from sniperbot.db import repo
    from sniperbot.db.base import session_scope
    from sniperbot.sniper.weekly import pending_key

    bot, dp, session = bot_and_dispatcher
    await dp.feed_update(bot, update("/start"))
    async with session_scope() as db_session:
        await repo.set_state(db_session, pending_key(USER_ID), '[["sl", "45"]]')
        before = (await repo.get_settings(db_session, USER_ID, "rh")).stop_loss_pct

    letter = TgMessage(message_id=8, date=dt.datetime.now(dt.UTC),
                       chat=Chat(id=CHAT_ID, type="private"), text="письмо")
    await dp.feed_update(bot, Update(update_id=10, callback_query=CallbackQuery(
        id="2", chat_instance="c", data=TuneCB(action="skip").pack(),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="Vlad"),
        message=letter,
    )))

    async with session_scope() as db_session:
        assert (await repo.get_settings(db_session, USER_ID, "rh")).stop_loss_pct == before
        assert not await repo.get_state(db_session, pending_key(USER_ID))


async def test_the_chart_arrives_as_a_picture_and_leaves_the_card_alone(bot_and_dispatcher):
    """Карточку подменять нельзя: на ней кнопки продажи, и нужны они именно
    тогда, когда владелец смотрит на падающий график."""
    import datetime as dt
    from decimal import Decimal

    from aiogram.types import CallbackQuery

    from sniperbot.bot.keyboards import PosCB
    from sniperbot.chart import pack
    from sniperbot.db.base import session_scope
    from sniperbot.db.models import Position
    from sniperbot.utils.fmt import to_wei

    bot, dp, session = bot_and_dispatcher
    await dp.feed_update(bot, update("/start"))

    async with session_scope() as db_session:
        position = Position(
            user_id=USER_ID, chain="rh", token_address="0x" + "a" * 40, token_symbol="PAW",
            token_decimals=18, router_address="0x" + "c" * 40, amount_wei=to_wei(1000),
            native_spent_wei=to_wei("0.008"), status="open",
            entry_price=Decimal("1"), last_price=Decimal("2"),
            price_track=pack([(0, Decimal("1")), (60, Decimal("2")), (120, Decimal("3"))]),
        )
        db_session.add(position)
        await db_session.flush()
        pid = position.id

    card = TgMessage(message_id=11, date=dt.datetime.now(dt.UTC),
                     chat=Chat(id=CHAT_ID, type="private"), text="карточка позиции")
    session.sent.clear()
    await dp.feed_update(bot, Update(update_id=21, callback_query=CallbackQuery(
        id="2", chat_instance="c", data=PosCB(action="chart", pid=pid).pack(),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="Vlad"),
        message=card,
    )))

    assert any("PAW" in text for text in session.sent), "картинка должна прийти с подписью"
    assert any("от входа" in text for text in session.sent), "без цифр график мало что говорит"
