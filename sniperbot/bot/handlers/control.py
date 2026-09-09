"""Управление ботом: включение снайпинга, тестовый режим, паника, кошелёк."""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import MenuCB, main_menu
from sniperbot.bot.ui import reply
from sniperbot.chain.wallet import WalletError
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User, utcnow
from sniperbot.settings_registry import effective_gas_multiplier
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="control")


# ------------------------------------------------------------ вкл / выкл
@router.message(Command("on", "snipe"))
async def cmd_on(message: Message, user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    async with session_scope() as session:
        stored = await repo.get_settings(session, user.id, chain.key)
        stored.auto_snipe = True
        stored.risk_reset_at = utcnow()      # /on снимает стоп по убыткам подряд
    cfg.auto_snipe = True
    mode = "🧪 ТЕСТ" if user.dry_run else "💰 БОЕВОЙ"
    await reply(
        message,
        f"🎯 <b>Автоснайп включён</b> · {mode}\n"
        f"Сеть: {esc(chain.name)}\n"
        f"Сумма входа: {fmt_amount(cfg.buy_amount)} {chain.native_symbol}\n"
        f"TP +{cfg.take_profit_pct}% · SL −{cfg.stop_loss_pct}%"
        + (f" · трейлинг {cfg.trailing_stop_pct}%" if cfg.trailing_stop_pct else "")
        + "\nСчётчик убытков подряд сброшен.",
        main_menu(chain.name, True),
    )


@router.message(Command("off"))
async def cmd_off(message: Message, user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    async with session_scope() as session:
        stored = await repo.get_settings(session, user.id, chain.key)
        stored.auto_snipe = False
        open_count = await repo.count_open_positions(session, user.id, chain.key)
    cfg.auto_snipe = False
    await reply(
        message,
        f"⛔️ <b>Автоснайп выключен</b> ({esc(chain.name)})\n"
        f"Открытых позиций: {open_count} — они продолжают вестись, "
        "тейк-профит и стоп-лосс работают.",
        main_menu(chain.name, False),
    )


@router.message(Command("dry"))
async def cmd_dry(message: Message, user: User) -> None:
    async with session_scope() as session:
        stored = await session.get(User, user.id)
        stored.dry_run = not bool(stored.dry_run)
        value = stored.dry_run
    user.dry_run = value
    if value:
        text = ("🧪 <b>Тестовый режим включён</b>\n\n"
                "Покупки и продажи считаются по реальным котировкам, но транзакции "
                "не отправляются и деньги не тратятся. Позиции помечаются как бумажные, "
                "тейк-профит и стоп-лосс по ним отрабатывают как обычно.\n\n"
                "Отчёт по ним: <code>/pnl test</code>")
    else:
        text = ("💰 <b>Боевой режим</b>\n\nСделки идут на реальные деньги. "
                "Бумажные позиции остаются в истории отдельно.")
    await reply(message, text)


# ---------------------------------------------------------------- паника
@router.message(Command("panic"))
async def cmd_panic(message: Message, command: CommandObject, ctx: BotContext,
                    user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    if (command.args or "").strip().lower() not in {"now", "confirm", "да"}:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚨 Да, продать всё", callback_data="panic:go"),
            InlineKeyboardButton(text="Отмена", callback_data=MenuCB(section="main").pack()),
        ]])
        async with session_scope() as session:
            positions = await repo.open_positions(session, user_id=user.id)
        await reply(
            message,
            f"🚨 <b>Аварийная продажа</b>\n\nВыключу автоснайп и продам все открытые позиции "
            f"({len(positions)} шт.) по рынку. Отменить будет нельзя.\n\n"
            "Быстрый вариант без вопросов: <code>/panic now</code>",
            kb,
        )
        return
    await _panic(message, ctx, user)


@router.callback_query(F.data == "panic:go")
async def cb_panic(callback: CallbackQuery, ctx: BotContext, user: User) -> None:
    await callback.answer("Продаю…")
    await _panic(callback.message, ctx, user)


async def _panic(message: Message, ctx: BotContext, user: User) -> None:
    async with session_scope() as session:
        for chain_key in ctx.active_chain_keys:
            stored = await repo.get_settings(session, user.id, chain_key)
            stored.auto_snipe = False
        positions = await repo.open_positions(session, user_id=user.id)

    status = await reply(message, f"🚨 Автоснайп выключен. Продаю позиций: {len(positions)}…")
    sold, failed = 0, []
    for position in positions:
        async with session_scope() as session:
            cfg = await repo.get_settings(session, user.id, position.chain)
        try:
            result = await ctx.trader.sell(user, position, cfg=cfg, percent=100, reason="panic")
        except Exception as exc:  # noqa: BLE001 - продолжаем по остальным позициям
            log.exception("Паника: позиция #%s не продана: %s", position.id, exc)
            failed.append((position, str(exc)))
            continue
        if result.ok:
            sold += 1
        else:
            failed.append((position, result.error or "неизвестная ошибка"))

    lines = [f"🚨 <b>Готово.</b> Продано позиций: {sold} из {len(positions)}"]
    for position, error in failed:
        lines.append(f"⚠️ #{position.id} {esc(position.token_symbol)}: {esc(error)}")
    if failed:
        lines.append("\nНепроданное можно попробовать вручную: /positions")
    await status.edit_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------------- кошелёк
@router.message(Command("balance"))
async def cmd_balance(message: Message, ctx: BotContext, user: User) -> None:
    lines = [f"💰 <b>Баланс</b>\n<code>{user.wallet_address}</code>\n"]
    for key in ctx.active_chain_keys:
        chain = ctx.chain(key)
        try:
            raw = await ctx.registry.get(key).native_balance(user.wallet_address)
            lines.append(f"{esc(chain.name)}: <b>{fmt_amount(from_wei(raw, chain.native_decimals))} "
                         f"{chain.native_symbol}</b>")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{esc(chain.name)}: недоступно ({esc(str(exc)[:60])})")
    await reply(message, "\n".join(lines))


@router.message(Command("newwallet"))
async def cmd_newwallet(message: Message, command: CommandObject, ctx: BotContext, user: User) -> None:
    if (command.args or "").strip().lower() not in {"confirm", "да"}:
        await reply(
            message,
            "🆕 <b>Новый кошелёк</b>\n\n"
            f"Текущий адрес:\n<code>{user.wallet_address}</code>\n\n"
            "⚠️ Старый приватный ключ будет удалён из бота безвозвратно. "
            "Сначала выведите средства (/withdraw) или сохраните ключ (/export).\n\n"
            "Подтвердить: <code>/newwallet confirm</code>",
        )
        return

    async with session_scope() as session:
        stored = await session.get(User, user.id)
        address, encrypted = ctx.wallets.generate(user.id)
        stored.wallet_address = address
        stored.encrypted_key = encrypted
        stored.key_fingerprint = ctx.wallets.vault.fingerprint()
        for chain_key in ctx.active_chain_keys:
            cfg = await repo.get_settings(session, user.id, chain_key)
            cfg.last_native_balance = 0
            cfg.deposit_synced = False
    user.wallet_address = address
    await reply(message, f"✅ <b>Новый кошелёк создан</b>\n<code>{address}</code>\n\n"
                         "Пополните его, чтобы торговать.")


@router.message(Command("import"))
async def cmd_import(message: Message, command: CommandObject, ctx: BotContext, user: User) -> None:
    key = (command.args or "").strip()
    if not key:
        await reply(
            message,
            "🔑 <b>Импорт кошелька</b>\n\n"
            "Пришлите: <code>/import ПРИВАТНЫЙ_КЛЮЧ</code>\n\n"
            "Сообщение с ключом бот удалит сразу после импорта. "
            "Текущий кошелёк будет заменён — сохраните его ключ (/export), если он нужен.",
        )
        return

    try:
        address, encrypted = ctx.wallets.import_key(user.id, key)
    except WalletError as exc:
        await _delete(message)
        await reply(message, f"❌ {esc(exc)}")
        return

    async with session_scope() as session:
        stored = await session.get(User, user.id)
        stored.wallet_address = address
        stored.encrypted_key = encrypted
        stored.key_fingerprint = ctx.wallets.vault.fingerprint()
        for chain_key in ctx.active_chain_keys:
            cfg = await repo.get_settings(session, user.id, chain_key)
            cfg.last_native_balance = 0
            cfg.deposit_synced = False
    user.wallet_address = address

    await _delete(message)
    await reply(message, f"✅ <b>Кошелёк импортирован</b>\n<code>{address}</code>\n\n"
                         "Сообщение с ключом удалено.")


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 - у бота может не быть прав
        log.debug("Не удалил сообщение с ключом: %s", exc)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Сбрасывает ожидание ввода — на случай, если бот «завис» на вопросе."""
    was_waiting = await state.get_state() is not None
    await state.clear()
    await reply(
        message,
        "❌ Ввод отменён." if was_waiting else "Нечего отменять — бот ничего не ждёт.",
    )


@router.message(Command("id"))
async def cmd_id(message: Message, user: User, is_admin: bool = False) -> None:
    """Свой Telegram ID — его вписывают в ADMIN_IDS и ALLOWED_USER_IDS."""
    await reply(
        message,
        f"🪪 Ваш Telegram ID: <code>{user.id}</code>\n"
        f"Права администратора: {'есть' if is_admin else 'нет'}\n\n"
        "Этот ID вписывается в <code>ADMIN_IDS</code> и <code>ALLOWED_USER_IDS</code> файла .env.",
    )



@router.message(Command("version"))
async def cmd_version(message: Message, ctx: BotContext) -> None:
    from sqlalchemy import select

    from sniperbot.bot.startup import human_duration, stale_build_warning
    from sniperbot.db.models import BotRun
    from sniperbot.version import build_info

    running = ctx.build            # сборка, с которой процесс запустился
    on_disk = build_info()         # что лежит на диске прямо сейчас

    lines = [f"🤖 <b>Сборка</b>\n{esc(running.short())}"]
    if running.branch:
        lines.append(f"Ветка: <code>{esc(running.branch)}</code>")

    warning = stale_build_warning(running, on_disk)
    if warning:
        lines.append("\n" + warning)

    async with session_scope() as session:
        runs = list((await session.scalars(
            select(BotRun).order_by(BotRun.id.desc()).limit(5))).all())
    if runs:
        current = runs[0]
        uptime = utcnow() - (current.started_at if current.started_at.tzinfo
                             else current.started_at.replace(tzinfo=dt.UTC))
        lines.append(f"Работает без перерыва: <b>{human_duration(uptime)}</b>")
        lines.append("\n<b>Последние запуски</b>")
        for run in runs:
            mark = "▸" if run is current else "·"
            status = "" if run.clean_shutdown or run is current else " ⚠️ аварийно"
            lines.append(f"{mark} {run.started_at:%d.%m %H:%M} · "
                         f"<code>{esc(run.commit or '—')}</code>{status}")
    await reply(message, "\n".join(lines))


# ----------------------------------------------------------------- сводка
@router.message(Command("speed"))
async def cmd_speed(message: Message, user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    multiplier = effective_gas_multiplier(cfg)
    route = {"auto": "авто (самый ликвидный пул)", "v2": "только Uniswap V2", "v3": "только Uniswap V3"}
    await reply(
        message,
        f"⚡️ <b>Скорость и маршрут</b> — {esc(chain.name)}\n\n"
        f"Режим: <b>{'🧪 ТЕСТ' if user.dry_run else '💰 БОЕВОЙ'}</b>\n"
        f"Автоснайп: <b>{'вкл' if cfg.auto_snipe else 'выкл'}</b>\n"
        f"Сумма входа: <b>{fmt_amount(cfg.buy_amount)} {chain.native_symbol}</b>\n"
        f"Газ: <b>{cfg.gas_mode}</b> (×{multiplier:g}), лимит {cfg.gas_limit:,}".replace(",", " ") + "\n"
        + (f"Приоритетная комиссия: <b>{fmt_amount(cfg.priority_fee_gwei)} gwei</b>\n"
           if chain.eip1559 else "")
        + f"Проскальзывание: <b>{cfg.slippage_bps / 100:g}%</b>\n"
        f"Маршрут: <b>{route.get(cfg.dex_route, cfg.dex_route)}</b>\n"
        f"Пауза между покупками: <b>{cfg.cooldown_seconds} c</b>\n\n"
        "Изменить: <code>/set gasmode turbo</code>, <code>/set route v3</code>, "
        "<code>/set buy 0.05</code>",
    )
