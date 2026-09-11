"""Сборка и запуск Telegram-бота вместе с фоновыми задачами."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent

from sniperbot.access import STATE_EXTRA, STATE_MODE, AccessPolicy, parse_ids
from sniperbot.bot.context import BotContext
from sniperbot.bot.handlers import build_router
from sniperbot.bot.middlewares import AccessMiddleware, UserMiddleware
from sniperbot.bot.startup import collect_stats, record_start, record_stop, render_restart
from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.wallet import WalletService
from sniperbot.config import Settings, get_chains, get_settings
from sniperbot.db.base import close_db, init_db, session_scope
from sniperbot.fees import STATE_KEY as FEES_STATE_KEY
from sniperbot.fees import FeeSettings
from sniperbot.notify import TelegramNotifier
from sniperbot.security.keyvault import KeyVault
from sniperbot.sniper.deposits import DepositWatcher
from sniperbot.sniper.engine import SniperEngine
from sniperbot.sniper.executor import Trader
from sniperbot.sniper.positions import PositionMonitor
from sniperbot.utils.fmt import esc
from sniperbot.version import build_info

log = logging.getLogger(__name__)

# Команды, видимые всем. Админские живут отдельным списком ниже: Telegram умеет
# показывать разные наборы разным чатам, и лишнего в меню пользователя быть не должно.
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
    BotCommand(command="recover", description="Подобрать потерянную позицию"),
    BotCommand(command="report", description="Отчёт за всё время + файл"),
    BotCommand(command="pnl", description="Сделки одного режима + файл"),
    BotCommand(command="edge", description="Есть ли преимущество"),
    BotCommand(command="optimize", description="Подобрать TP/SL по истории"),
    BotCommand(command="winrate", description="Как получить нужный % плюсовых"),
    BotCommand(command="stats", description="Качество фильтров и поток"),
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
    BotCommand(command="preset", description="Готовый набор настроек"),
    BotCommand(command="config", description="Все настройки"),
    BotCommand(command="set", description="Изменить настройку"),
    BotCommand(command="speed", description="Режим газа и маршрут"),
    BotCommand(command="settings", description="Настройки кнопками"),
    BotCommand(command="blacklist", description="Чёрный список токенов"),
    BotCommand(command="withdraw", description="Вывод средств"),
    BotCommand(command="export", description="Показать приватный ключ"),
    BotCommand(command="newwallet", description="Создать новый кошелёк"),
    BotCommand(command="import", description="Привязать свой кошелёк"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="history", description="История сделок"),
    BotCommand(command="chain", description="Переключить сеть"),
    BotCommand(command="id", description="Мой Telegram ID"),
    BotCommand(command="cancel", description="Отменить ввод"),
    BotCommand(command="version", description="Версия и перезапуски"),
    BotCommand(command="ref", description="Пригласить друзей и снять комиссию"),
    BotCommand(command="help", description="Помощь"),
]

# Видны только тем, чьи id перечислены в ADMIN_IDS.
ADMIN_COMMANDS = [
    BotCommand(command="fees", description="🔒 Комиссии: включить, выключить, ставки"),
    BotCommand(command="treasury", description="🔒 Кошелёк комиссий: баланс и вывод"),
    BotCommand(command="exempt", description="🔒 Освободить пользователя от комиссий"),
    BotCommand(command="access", description="🔒 Кому открыт бот"),
    BotCommand(command="users", description="🔒 Список пользователей"),
    BotCommand(command="userinfo", description="🔒 Карточка пользователя"),
    BotCommand(command="ban", description="🔒 Заблокировать пользователя"),
    BotCommand(command="unban", description="🔒 Разблокировать"),
    BotCommand(command="broadcast", description="🔒 Сообщение всем"),
    BotCommand(command="health", description="🔒 Живы ли сканеры и ноды"),
    BotCommand(command="usage", description="🔒 Расход RPC и размер базы"),
    BotCommand(command="latency", description="🔒 Задержки эндпоинтов"),
    BotCommand(command="logs", description="🔒 Журнал операций"),
    BotCommand(command="restart", description="🔒 Перезапуск бота"),
]


async def load_access(settings: Settings) -> AccessPolicy:
    """Собирает правило доступа: .env как стартовое состояние, база — как решение.

    Команда /access должна переживать перезапуск, поэтому её выбор хранится в
    базе и перекрывает ALLOWED_USER_IDS. Файл с ключами при этом не трогается.
    """
    from sniperbot.db import repo

    async with session_scope() as session:
        override = await repo.get_state(session, STATE_MODE)
        extra = await repo.get_state(session, STATE_EXTRA)
    return AccessPolicy(
        admins=frozenset(settings.admin_ids),
        env_allowed=frozenset(settings.allowed_user_ids),
        extra=parse_ids(extra),
        override=override,
    )


async def load_fees(settings: Settings) -> FeeSettings:
    """Комиссии: .env как стартовое значение, решение команды /fees — сверху."""
    from sniperbot.db import repo
    from sniperbot.sniper.executor import fee_settings_from

    fees = fee_settings_from(settings)
    async with session_scope() as session:
        stored = await repo.get_state(session, FEES_STATE_KEY)
    fees.apply_state(stored)
    return fees


async def publish_commands(bot: Bot, admins: set[int]) -> None:
    """Ставит меню команд: общее всем и расширенное — администраторам.

    Список у пользователя должен содержать только то, что ему доступно: команда,
    которая всё равно ответит «только для администратора», в меню лишь мешает.
    """
    await bot.set_my_commands(COMMANDS)
    for admin_id in admins:
        try:
            await bot.set_my_commands(
                [*COMMANDS, *ADMIN_COMMANDS],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as exc:  # noqa: BLE001 - админ мог не запускать бота
            log.debug("Меню для администратора %s не поставлено: %s", admin_id, exc)


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
    fees = await load_fees(settings)
    trader = Trader(registry, wallets, settings, fees=fees)

    async def announce_late_result(user, result) -> None:  # noqa: ANN001 - User, TradeResult
        """Итог транзакции, которая подтвердилась уже после ответа боту."""
        chain = registry.config(user.active_chain)
        if result.ok:
            text = (f"✅ <b>Подтвердилась</b> покупка {esc(result.token_symbol)}\n"
                    f"Позиция #{result.position_id} открыта — автопродажа работает.\n"
                    f"<a href='{result.explorer_url}'>Транзакция</a>")
        else:
            text = (f"❌ Отложенная покупка {esc(result.token_symbol)} не удалась:\n"
                    f"{esc(result.error or 'причина неизвестна')}")
            if result.tx_hash:
                text += f"\n<a href='{chain.tx_url(result.tx_hash)}'>Транзакция</a>"
        await notifier.send(user.id, text)

    trader.on_late_result = announce_late_result
    engine = SniperEngine(registry, trader, wallets, notifier, settings)
    monitor = PositionMonitor(registry, trader, notifier, settings)
    deposits = DepositWatcher(registry, notifier, settings, wallets=wallets, trader=trader)

    running_build = build_info()
    policy = await load_access(settings)
    ctx = BotContext(
        settings=settings, registry=registry, wallets=wallets,
        trader=trader, engine=engine, notifier=notifier, build=running_build,
        access=policy, fees=fees,
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    access = AccessMiddleware(policy)
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

    report = await record_start(running_build)
    log.info("Сборка: %s (%s)", report.info.short(), report.info.source)

    try:
        await publish_commands(bot, settings.admin_ids)
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
        await trader.close()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        await bot.session.close()
        await registry.close_all()
        await close_db()
