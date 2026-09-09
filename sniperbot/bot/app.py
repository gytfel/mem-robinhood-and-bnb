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
from sniperbot.bot.startup import collect_stats, record_start, record_stop, render_restart
from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.wallet import WalletService
from sniperbot.config import Settings, get_chains, get_settings
from sniperbot.db.base import close_db, init_db, session_scope
from sniperbot.notify import TelegramNotifier
from sniperbot.security.keyvault import KeyVault
from sniperbot.sniper.deposits import DepositWatcher
from sniperbot.sniper.engine import SniperEngine
from sniperbot.sniper.executor import Trader
from sniperbot.sniper.positions import PositionMonitor
from sniperbot.version import build_info

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
    BotCommand(command="report", description="Отчёт за всё время + файл"),
    BotCommand(command="pnl", description="Сделки одного режима + файл"),
    BotCommand(command="edge", description="Есть ли преимущество"),
    BotCommand(command="optimize", description="Подобрать TP/SL по истории"),
    BotCommand(command="stats", description="Поток токенов и фильтры"),
    BotCommand(command="ab", description="A/B-тест настроек"),
    BotCommand(command="creators", description="Репутация создателей"),
    BotCommand(command="trending", description="Кто разгоняется сейчас"),
    BotCommand(command="watch", description="Следить за токеном"),
    BotCommand(command="trends", description="Горячие темы"),
    BotCommand(command="bundles", description="Конкуренция за вход"),
    BotCommand(command="paths", description="Где торгуется токен"),
    BotCommand(command="route", description="Площадка: v2/v3/auto"),
    BotCommand(command="calibrate", description="Диагностика токена"),
    BotCommand(command="tip", description="Совет по газу"),
    BotCommand(command="config", description="Все настройки"),
    BotCommand(command="set", description="Изменить настройку"),
    BotCommand(command="speed", description="Режим газа и маршрут"),
    BotCommand(command="settings", description="Настройки кнопками"),
    BotCommand(command="blacklist", description="Чёрный список токенов"),
    BotCommand(command="withdraw", description="Вывод средств"),
    BotCommand(command="history", description="История сделок"),
    BotCommand(command="chain", description="Переключить сеть"),
    BotCommand(command="id", description="Мой Telegram ID"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="version", description="Версия и перезапуски"),
    BotCommand(command="help", description="Помощь"),
]


async def announce_restart(ctx: BotContext, text: str) -> None:
    """Рассылает сообщение о перезапуске тем, кто должен о нём знать."""
    from sniperbot.db import repo

    admins = ctx.settings.admin_ids
    async with session_scope() as session:
        users = await repo.all_users(session, with_wallet=False)

    # Есть админы — пишем им; иначе это личный бот, и знать должен владелец.
    recipients = [user for user in users if user.id in admins] if admins else users
    sent = 0
    for user in recipients:
        if not getattr(user, "notify_restart", True):
            continue
        await ctx.notifier.send(user.id, text)
        sent += 1
    if not sent:
        log.info("Уведомление о перезапуске никому не отправлено "
                 "(нет получателей или отключено настройкой)")


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

    report = await record_start(build_info())
    log.info("Сборка: %s (%s)", report.info.short(), report.info.source)

    try:
        await bot.set_my_commands(COMMANDS)
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)

        report.stats = await collect_stats(registry, ctx.active_chain_keys)
        await announce_restart(ctx, render_restart(report))

        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        await record_stop(report.run_id)
        monitor.stop()
        deposits.stop()
        await engine.stop()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await bot.session.close()
        await registry.close_all()
        await close_db()
