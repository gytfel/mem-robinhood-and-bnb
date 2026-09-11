"""Позиции: список, карточка, ручная продажа, история сделок."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import MenuCB, PosCB, back_button, position_actions, positions_list
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.bot.views import render_position, render_positions_list
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.sniper.positions import PositionMonitor
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="positions")


@router.message(Command("positions"))
async def cmd_positions(message: Message, ctx: BotContext, user: User) -> None:
    text, markup = await _positions_view(ctx, user)
    await reply(message, text, markup)


@router.callback_query(MenuCB.filter(F.section == "positions"))
async def cb_positions(callback: CallbackQuery, ctx: BotContext, user: User) -> None:
    # Сначала гасим часики Telegram, потом собираем экран: иначе кнопка выглядит
    # зависшей всё время, пока идут запросы к ноде.
    await callback.answer()
    text, markup = await _positions_view(ctx, user)
    await safe_edit(callback, text, markup)


@router.callback_query(PosCB.filter(F.action == "view"))
async def cb_position(callback: CallbackQuery, callback_data: PosCB, ctx: BotContext, user: User) -> None:
    async with session_scope() as session:
        position = await repo.find_position(session, callback_data.pid, user.id)
    if position is None:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    chain = ctx.chain(position.chain)
    await callback.answer()
    price = await _price(ctx, position)
    await safe_edit(callback, render_position(position, chain, price), position_actions(position.id))


@router.callback_query(PosCB.filter(F.action == "sell"))
async def cb_sell(
    callback: CallbackQuery, callback_data: PosCB, ctx: BotContext, user: User
) -> None:
    async with session_scope() as session:
        position = await repo.find_position(session, callback_data.pid, user.id)
        cfg = await repo.get_settings(session, user.id, position.chain) if position else None
    if position is None or cfg is None:
        await callback.answer("Позиция не найдена", show_alert=True)
        return
    await callback.answer("Продаю…")
    await _do_sell(callback.message, ctx, user, position, cfg, callback_data.pct)


@router.message(Command("sell"))
async def cmd_sell(
    message: Message, command: CommandObject, ctx: BotContext, user: User
) -> None:
    args = (command.args or "").split()
    if not args or not args[0].lstrip("#").isdigit():
        await reply(message, "Использование: <code>/sell &lt;id позиции&gt; [процент]</code>")
        return
    position_id = int(args[0].lstrip("#"))
    percent = int(args[1]) if len(args) > 1 and args[1].isdigit() else 100

    async with session_scope() as session:
        position = await repo.find_position(session, position_id, user.id)
        cfg = await repo.get_settings(session, user.id, position.chain) if position else None
    if position is None or cfg is None:
        await reply(message, "❌ Позиция не найдена.")
        return
    await _do_sell(message, ctx, user, position, cfg, percent)


@router.message(Command("apply", "применить"))
async def cmd_apply(message: Message, command: CommandObject, ctx: BotContext, user: User,
                    cfg: ChainSettings, chain: ChainConfig) -> None:
    """Переносит текущие правила выхода на уже открытые позиции.

    Позиция запоминает правила на момент покупки — так честнее для статистики,
    но человек, поменявший тейк, ждёт, что он подействует и на то, что открыто.
    """
    from sniperbot.sniper.executor import copy_exit_rules

    args = (command.args or "").strip().lstrip("#")
    only = int(args) if args.isdigit() else None

    async with session_scope() as session:
        positions = await repo.open_positions(session, user_id=user.id, chain=chain.key)
        touched = []
        for position in positions:
            if only is not None and position.id != only:
                continue
            if copy_exit_rules(cfg, position):
                touched.append(position)

    if only is not None and not any(p.id == only for p in positions):
        await reply(message, f"❌ Открытой позиции #{only} в сети {esc(chain.name)} нет.")
        return
    if not positions:
        await reply(message, f"Открытых позиций в сети {esc(chain.name)} нет.")
        return
    if not touched:
        await reply(message, "Все открытые позиции уже работают по текущим настройкам.")
        return

    names = ", ".join(f"#{position.id} {esc(position.token_symbol)}" for position in touched[:10])
    await reply(
        message,
        f"✅ Новые правила выхода применены к {len(touched)} позиции(ям): {names}\n\n"
        f"{esc(_exit_summary(cfg))}\n\n"
        "<i>Уже сработавшие ступени не повторяются: то, что продано, продано.</i>",
    )


def _exit_summary(cfg: ChainSettings) -> str:
    from sniperbot.settings_registry import find

    parts = [f"Тейк: {find('tp').display(cfg)}"]
    if cfg.stop_loss_pct:
        parts.append(f"стоп −{cfg.stop_loss_pct}%")
    if cfg.trailing_stop_pct:
        parts.append(f"трейлинг {cfg.trailing_stop_pct}%")
    if cfg.secure_pct:
        parts.append(f"возврат вложенного +{cfg.secure_pct}%")
    return " · ".join(parts)


@router.message(Command("hide", "writeoff", "списать"))
async def cmd_hide(message: Message, command: CommandObject, ctx: BotContext,
                   user: User) -> None:
    """Убирает из активных позицию, которую невозможно продать."""
    args = (command.args or "").split()
    if not args or not args[0].lstrip("#").isdigit():
        await reply(
            message,
            "Использование: <code>/hide &lt;id позиции&gt;</code>\n\n"
            "Убирает позицию из активных, если продать её не получается: "
            "она перестаёт занимать лимит и дёргать монитор.\n"
            "В отчётах остаётся убытком — деньги потрачены на самом деле. "
            "Вернуть можно командой <code>/recover &lt;адрес токена&gt;</code>.",
        )
        return

    position_id = int(args[0].lstrip("#"))
    async with session_scope() as session:
        position = await repo.find_position(session, position_id, user.id)
        if position is None:
            await reply(message, "❌ Позиция не найдена.")
            return
        if position.status != "open":
            await reply(message, f"Позиция #{position_id} и так закрыта.")
            return
        token, symbol = position.token_address, position.token_symbol
        await repo.write_off_position(session, position_id, user.id)

    await reply(
        message,
        f"🪦 Позиция #{position_id} ({esc(symbol)}) убрана из активных.\n"
        "Лимит она больше не занимает, монитор её не трогает.\n\n"
        f"Если токен снова станет продаваемым: <code>/recover {token}</code>",
    )


@router.message(Command("history"))
async def cmd_history(message: Message, ctx: BotContext, user: User) -> None:
    async with session_scope() as session:
        positions = await repo.closed_positions(session, user.id, limit=15)
        spent, returned = await repo.total_pnl(session, user.id)
    if not positions:
        await reply(message, "🧾 Закрытых сделок пока нет.")
        return

    lines = ["🧾 <b>История сделок</b>\n"]
    for position in positions:
        chain = ctx.chain(position.chain) if position.chain in ctx.registry.configs else None
        symbol = chain.native_symbol if chain else ""
        pnl = position.pnl_native_wei
        icon = "🟢" if pnl >= 0 else "🔴"
        when = (position.closed_at or position.opened_at).strftime("%d.%m %H:%M")
        lines.append(
            f"{icon} {when} <b>{esc(position.token_symbol)}</b>: "
            f"{fmt_amount(from_wei(pnl))} {symbol} "
            f"({fmt_amount(from_wei(position.native_spent_wei))} → {fmt_amount(from_wei(position.native_returned_wei))})"
        )
    total = returned - spent
    lines.append(
        f"\n<b>Итого:</b> {'🟢' if total >= 0 else '🔴'} {fmt_amount(from_wei(total))} "
        f"({fmt_amount(from_wei(spent))} вложено)"
    )
    await reply(message, "\n".join(lines), back_button())


# ---------------------------------------------------------------- внутренности
async def _positions_view(ctx: BotContext, user: User):
    async with session_scope() as session:
        positions = await repo.open_positions(session, user_id=user.id)
    names = {key: cfg.name for key, cfg in ctx.registry.configs.items()}
    return render_positions_list(positions, names), positions_list(positions)


async def _price(ctx: BotContext, position):
    monitor = PositionMonitor(ctx.registry, ctx.trader, ctx.notifier, ctx.settings)
    try:
        return await monitor.current_price(position)
    except Exception as exc:  # noqa: BLE001 - пул мог опустеть
        log.debug("Не смог оценить позицию #%s: %s", position.id, exc)
        return None


async def _do_sell(
    message: Message, ctx: BotContext, user: User, position, cfg: ChainSettings, percent: int
) -> None:
    percent = max(1, min(100, percent))
    chain: ChainConfig = ctx.chain(position.chain)
    status = await reply(message, f"⏳ Продаю {percent}% {esc(position.token_symbol)}…")
    try:
        result = await ctx.trader.sell(user, position, cfg=cfg, percent=percent, reason="manual")
    except Exception as exc:  # noqa: BLE001
        log.exception("Продажа не удалась: %s", exc)
        await status.edit_text(f"❌ Ошибка продажи: {esc(exc)}", parse_mode="HTML")
        return

    if not result.ok:
        await status.edit_text(f"❌ {esc(result.error)}", parse_mode="HTML")
        return
    await status.edit_text(
        f"✅ Продано {percent}% <b>{esc(result.token_symbol)}</b>\n"
        f"Получено: {fmt_amount(from_wei(result.amount_out, chain.native_decimals))} {chain.native_symbol}\n"
        f"<a href='{result.explorer_url}'>Транзакция</a>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
