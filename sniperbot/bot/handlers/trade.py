"""Ручная покупка: проверка токена по адресу и исполнение сделки."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import BuyCB, buy_menu, cancel_kb, main_menu
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.bot.views import render_report
from sniperbot.config import ChainConfig
from sniperbot.db.models import ChainSettings, User
from sniperbot.sniper.safety import analyze_best
from sniperbot.utils.evm import extract_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, parse_decimal, to_wei

log = logging.getLogger(__name__)

router = Router(name="trade")


class BuyStates(StatesGroup):
    amount = State()


@router.message(Command("check"))
async def cmd_check(
    message: Message, command: CommandObject, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig,
) -> None:
    token = extract_address(command.args or "")
    if not token:
        await reply(message, "Использование: <code>/check &lt;адрес токена&gt;</code>")
        return
    await show_token(message, ctx, cfg, chain, token)


@router.message(Command("buy"))
async def cmd_buy(
    message: Message, command: CommandObject, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig,
) -> None:
    args = (command.args or "").split()
    token = extract_address(args[0]) if args else None
    if not token:
        await reply(message, "Использование: <code>/buy &lt;адрес токена&gt; [сумма]</code>")
        return
    amount = parse_decimal(args[1]) if len(args) > 1 else cfg.buy_amount
    if amount is None or amount <= 0:
        await reply(message, "❌ Некорректная сумма.")
        return
    await execute_buy(message, ctx, user, cfg, chain, token, amount)


@router.callback_query(BuyCB.filter())
async def cb_buy(
    callback: CallbackQuery, callback_data: BuyCB, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig, state: FSMContext,
) -> None:
    token = callback_data.token
    action = callback_data.amount

    if action == "recheck":
        await callback.answer("Перепроверяю…")
        await show_token(callback.message, ctx, cfg, chain, token, edit_event=callback)
        return

    if action == "custom":
        await state.set_state(BuyStates.amount)
        await state.update_data(token=token)
        await safe_edit(
            callback,
            f"✏️ На какую сумму в {chain.native_symbol} покупать?\nПришлите число, например <code>0.25</code>.",
            cancel_kb("main"),
        )
        await callback.answer()
        return

    amount = cfg.buy_amount if action == "default" else parse_decimal(action)
    if amount is None or amount <= 0:
        await callback.answer("Некорректная сумма", show_alert=True)
        return
    await callback.answer("Покупаю…")
    await execute_buy(callback.message, ctx, user, cfg, chain, token, amount)


@router.message(BuyStates.amount)
async def custom_amount(
    message: Message, state: FSMContext, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig,
) -> None:
    data = await state.get_data()
    token = data.get("token")
    amount = parse_decimal(message.text or "")
    if amount is None or amount <= 0:
        await reply(message, "❌ Не понял сумму, пришлите число.", cancel_kb("main"))
        return
    await state.clear()
    await execute_buy(message, ctx, user, cfg, chain, token, amount)


@router.message(F.text.regexp(r"0x[a-fA-F0-9]{40}"))
async def on_token_address(
    message: Message, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig
) -> None:
    token = extract_address(message.text)
    if not token:
        return
    await show_token(message, ctx, cfg, chain, token)


# ---------------------------------------------------------------- внутренности
async def show_token(
    message: Message, ctx: BotContext, cfg: ChainSettings, chain: ChainConfig,
    token: str, edit_event: CallbackQuery | None = None,
) -> None:
    """Собирает отчёт о токене и показывает карточку с кнопками покупки."""
    if edit_event is None:
        status = await reply(message, f"⏳ Проверяю токен в сети {esc(chain.name)}…")
    else:
        status = message

    client = ctx.registry.get(chain.key)
    if chain.default_router is None:
        await status.edit_text("❌ Для этой сети не настроен DEX-роутер.")
        return

    try:
        report = await analyze_best(
            client,
            token,
            amount_native_wei=to_wei(cfg.buy_amount, chain.native_decimals),
            settings=cfg,
            run_simulation=cfg.honeypot_check,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Анализ токена %s не удался: %s", token, exc)
        await status.edit_text(f"❌ Не удалось проверить токен: {esc(exc)}", parse_mode="HTML")
        return

    text = render_report(report, chain)
    markup = buy_menu(report.token.address, report.token.symbol, cfg.buy_amount, chain.native_symbol)
    try:
        await status.edit_text(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 - сообщение могло быть не наше
        await reply(message, text, markup)


async def execute_buy(
    message: Message, ctx: BotContext, user: User, cfg: ChainSettings,
    chain: ChainConfig, token: str, amount: Decimal,
) -> None:
    status = await reply(
        message, f"⏳ Покупаю на {fmt_amount(amount)} {chain.native_symbol}…"
    )
    try:
        result = await ctx.trader.buy(user, chain.key, token, amount, cfg=cfg, source="manual")
    except Exception as exc:  # noqa: BLE001
        log.exception("Покупка не удалась: %s", exc)
        await status.edit_text(f"❌ Ошибка покупки: {esc(exc)}", parse_mode="HTML")
        return

    if not result.ok:
        text = f"❌ Покупка не удалась:\n{esc(result.error)}"
        if result.explorer_url:
            text += f"\n\n<a href='{result.explorer_url}'>Транзакция</a>"
        await status.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        return

    received = from_wei(result.amount_out, result.token_decimals)
    await status.edit_text(
        f"✅ <b>Куплено {esc(result.token_symbol)}</b>\n\n"
        f"Потрачено: {fmt_amount(from_wei(result.amount_in, chain.native_decimals))} {chain.native_symbol}\n"
        f"Получено: {fmt_amount(received, 4)} {esc(result.token_symbol)}\n"
        f"Позиция: #{result.position_id}\n"
        f"<a href='{result.explorer_url}'>Транзакция</a>\n\n"
        f"Автопродажа: {'включена' if cfg.auto_sell else 'выключена'} "
        f"(TP +{cfg.take_profit_pct}% / SL −{cfg.stop_loss_pct}%)",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=main_menu(chain.name, cfg.auto_snipe),
    )
