"""Сборка и запуск Telegram-бота вместе с фоновыми задачами."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from sniperbot.bot.context import BotContext
from sniperbot.bot.handlers import build_router
from sniperbot.bot.middlewares import AccessMiddleware, UserMiddleware
from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.wallet import WalletService
from sniperbot.config import Settings, get_chains, get_settings
from sniperbot.db.base import close_db, init_db
from sniperbot.notify import TelegramNotifier
from sniperbot.security.keyvault import KeyVault
from sniperbot.sniper.deposits import DepositWatcher
from sniperbot.sniper.engine import SniperEngine
from sniperbot.sniper.executor import Trader
from sniperbot.sniper.positions import PositionMonitor

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="on", description="Включить автоснайп"),
    BotCommand(command="off", description="Выключить автоснайп"),
    BotCommand(command="dry", description="Тестовый режим вкл/выкл"),
    BotCommand(command="panic", description="Продать всё немедленно"),
    BotCommand(command="wallet", description="Кошелёк и балансы"),
    BotCommand(command="balance", description="Только баланс"),
    BotCommand(command="buy", description="Купить токен"),
    BotCommand(command="sell", description="Продать позицию"),
    BotCommand(command="check", description="Проверить токен"),
    BotCommand(command="positions", description="Открытые позиции"),
    BotCommand(command="pnl", description="Отчёт по сделкам + CSV"),
    BotCommand(command="edge", description="Есть ли преимущество"),
    BotCommand(command="stats", description="Поток токенов и фильтры"),
    BotCommand(command="config", description="Все настройки"),
    BotCommand(command="set", description="Изменить настройку"),
    BotCommand(command="speed", description="Режим газа и маршрут"),
    BotCommand(command="settings", description="Настройки кнопками"),
    BotCommand(command="blacklist", description="Чёрный список токенов"),
    BotCommand(command="withdraw", description="Вывод средств"),
    BotCommand(command="history", description="История сделок"),
    BotCommand(command="chain", description="Переключить сеть"),
    BotCommand(command="help", description="Помощь"),
]


def build_registry(settings: Settings) -> ChainRegistry:
    chains = get_chains()
    active = [key for key, cfg in chains.items() if cfg.enabled and cfg.configured]
    if not active:
        raise SystemExit(
            "Ни одна сеть не настроена.\n"
            "Проверьте config/chains.json и переменные окружения "
            "(ENABLED_CHAINS, BSC_RPC_URLS, RH_*)."
        )
    log.info("Активные сети: %s", ", ".join(active))
    return ChainRegistry(chains)


async def run_bot() -> None:
    settings = get_settings()
    problems = settings.validate_runtime()
    if problems:
        raise SystemExit("Конфигурация неполная:\n- " + "\n- ".join(problems))

    await init_db(settings.resolved_database_url)
    registry = build_registry(settings)
    await registry.healthcheck_all()

    vault = KeyVault(settings.master_key)
    wallets = WalletService(vault)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = TelegramNotifier(bot)
    trader = Trader(registry, wallets, settings)
    engine = SniperEngine(registry, trader, wallets, notifier, settings)
    monitor = PositionMonitor(registry, trader, notifier, settings)
    deposits = DepositWatcher(registry, notifier, settings)

    ctx = BotContext(
        settings=settings, registry=registry, wallets=wallets,
        trader=trader, engine=engine, notifier=notifier,
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    access = AccessMiddleware(settings.allowed_user_ids, settings.admin_ids)
    users = UserMiddleware(ctx)
    for observer in (dp.message, dp.callback_query):
        observer.middleware(access)
        observer.middleware(users)
    dp.include_router(build_router())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Ошибка в хендлере: %s", event.exception)
        return True

    background = [
        asyncio.create_task(monitor.run(), name="position-monitor"),
        asyncio.create_task(deposits.run(), name="deposit-watcher"),
    ]
    engine.start()

    try:
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        monitor.stop()
        deposits.stop()
        await engine.stop()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await bot.session.close()
        await registry.close_all()
        await close_db()
