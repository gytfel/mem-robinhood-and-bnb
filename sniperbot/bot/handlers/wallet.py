"""Кошелёк: баланс, пополнение, вывод, экспорт ключа, история."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import MenuCB, WalletCB, back_button, cancel_kb, confirm_export, wallet_menu
from sniperbot.bot.texts import EXPORT_WARNING, WALLET_HINT
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.bot.views import render_wallet
from sniperbot.chain.wallet import WalletError
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.utils.evm import extract_address, is_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, parse_decimal, to_wei

log = logging.getLogger(__name__)

router = Router(name="wallet")


class WithdrawStates(StatesGroup):
    address = State()
    amount = State()


@router.message(Command("wallet"))
async def cmd_wallet(message: Message, ctx: BotContext, user: User, chain: ChainConfig) -> None:
    await reply(message, await render_wallet(ctx, user, chain), wallet_menu())


@router.callback_query(MenuCB.filter(F.section == "wallet"))
@router.callback_query(WalletCB.filter(F.action == "refresh"))
async def cb_wallet(callback: CallbackQuery, ctx: BotContext, user: User, chain: ChainConfig) -> None:
    await callback.answer()          # балансы читаются из сети — не держим кнопку нажатой
    await safe_edit(callback, await render_wallet(ctx, user, chain), wallet_menu())


@router.callback_query(WalletCB.filter(F.action == "deposit"))
async def cb_deposit(callback: CallbackQuery, user: User, chain: ChainConfig) -> None:
    text = (
        f"📥 <b>Пополнение</b>\n\n"
        f"Сеть: <b>{esc(chain.name)}</b>\n"
        f"Монета: <b>{chain.native_symbol}</b>\n\n"
        f"Адрес для перевода:\n<code>{user.wallet_address}</code>\n\n"
        f"{WALLET_HINT}\n\n"
        "Как только средства придут, бот пришлёт уведомление."
    )
    await safe_edit(callback, text, back_button("wallet"))
    await callback.answer()


@router.callback_query(WalletCB.filter(F.action == "history"))
async def cb_history(callback: CallbackQuery, ctx: BotContext, user: User) -> None:
    async with session_scope() as session:
        events = await repo.recent_wallet_events(session, user.id, limit=15)
    if not events:
        text = "🧾 Операций с кошельком пока не было."
    else:
        lines = ["🧾 <b>История кошелька</b>\n"]
        for event in events:
            symbol = ctx.chain(event.chain).native_symbol if event.chain in ctx.registry.configs else ""
            sign = "+" if event.kind == "deposit" else "−"
            when = event.created_at.strftime("%d.%m %H:%M")
            lines.append(f"{when} · {sign}{fmt_amount(from_wei(event.amount_wei))} {symbol}")
        text = "\n".join(lines)
    await safe_edit(callback, text, back_button("wallet"))
    await callback.answer()


# ------------------------------------------------------------------- экспорт
@router.message(Command("export"))
async def cmd_export(message: Message) -> None:
    await reply(message, EXPORT_WARNING, confirm_export())


@router.callback_query(WalletCB.filter(F.action == "export"))
async def cb_export(callback: CallbackQuery) -> None:
    await safe_edit(callback, EXPORT_WARNING, confirm_export())
    await callback.answer()


@router.callback_query(WalletCB.filter(F.action == "export_confirm"))
async def cb_export_confirm(callback: CallbackQuery, ctx: BotContext, user: User) -> None:
    try:
        private_key = ctx.wallets.export_key(user)
    except WalletError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    key = private_key if private_key.startswith("0x") else "0x" + private_key
    await safe_edit(
        callback,
        "🔐 <b>Приватный ключ</b>\n\n"
        f"<code>{key}</code>\n\n"
        "Сохраните его в надёжном месте и удалите это сообщение.\n"
        "Ключ можно импортировать в MetaMask/Trust Wallet.",
        back_button("wallet"),
    )
    await callback.answer()


# -------------------------------------------------------------------- вывод
@router.callback_query(WalletCB.filter(F.action == "withdraw"))
async def cb_withdraw(callback: CallbackQuery, state: FSMContext, chain: ChainConfig) -> None:
    await state.set_state(WithdrawStates.address)
    await safe_edit(
        callback,
        f"📤 <b>Вывод {chain.native_symbol}</b>\n\nПришлите адрес получателя (0x…):",
        cancel_kb("wallet"),
    )
    await callback.answer()


@router.message(WithdrawStates.address)
async def withdraw_address(message: Message, state: FSMContext, chain: ChainConfig) -> None:
    address = extract_address(message.text or "")
    if not address:
        await reply(message, "❌ Это не похоже на адрес. Пришлите адрес формата 0x…", cancel_kb("wallet"))
        return
    await state.update_data(address=address)
    await state.set_state(WithdrawStates.amount)
    await reply(
        message,
        f"Сколько {chain.native_symbol} вывести на\n<code>{address}</code>?\n\n"
        "Пришлите число или слово <b>всё</b>.",
        cancel_kb("wallet"),
    )


@router.message(WithdrawStates.amount)
async def withdraw_amount(
    message: Message, state: FSMContext, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig
) -> None:
    data = await state.get_data()
    address = data.get("address")
    raw = (message.text or "").strip().lower()
    amount: Decimal | None
    if raw in {"всё", "все", "all", "max"}:
        amount = None
    else:
        amount = parse_decimal(raw)
        if amount is None or amount <= 0:
            await reply(message, "❌ Не понял сумму. Пришлите число или «всё».", cancel_kb("wallet"))
            return
    await state.clear()
    await _do_withdraw(message, ctx, user, cfg, chain, address, amount)


@router.message(Command("withdraw"))
async def cmd_withdraw(
    message: Message, command: CommandObject, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig, state: FSMContext,
) -> None:
    args = (command.args or "").split()
    if not args:
        await state.set_state(WithdrawStates.address)
        await reply(message, f"📤 Вывод {chain.native_symbol}. Пришлите адрес получателя:", cancel_kb("wallet"))
        return
    address = extract_address(args[0])
    if not address or not is_address(address):
        await reply(message, "❌ Некорректный адрес получателя.")
        return
    amount = parse_decimal(args[1]) if len(args) > 1 else None
    if len(args) > 1 and amount is None:
        await reply(message, "❌ Некорректная сумма.")
        return
    await _do_withdraw(message, ctx, user, cfg, chain, address, amount)


async def _do_withdraw(
    message: Message, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig,
    address: str, amount: Decimal | None,
) -> None:
    status = await reply(message, "⏳ Отправляю транзакцию…")
    client = ctx.registry.get(chain.key)
    try:
        account = ctx.wallets.account(user)
        amount_wei = to_wei(amount, chain.native_decimals) if amount is not None else None
        sent, actual = await ctx.wallets.send_native(
            client, account, address, amount_wei, gas_multiplier=cfg.gas_multiplier
        )
    except WalletError as exc:
        await status.edit_text(f"❌ {esc(exc)}", parse_mode="HTML")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("Вывод не удался: %s", exc)
        await status.edit_text(f"❌ Не удалось вывести: {esc(exc)}", parse_mode="HTML")
        return

    async with session_scope() as session:
        await repo.log_wallet_event(
            session, user_id=user.id, chain=chain.key, kind="withdraw",
            amount_wei=actual, tx_hash=sent.tx_hash,
        )
    await status.edit_text(
        f"✅ Отправлено <b>{fmt_amount(from_wei(actual, chain.native_decimals))} {chain.native_symbol}</b>\n"
        f"на <code>{address}</code>\n\n"
        f"<a href='{chain.tx_url(sent.tx_hash)}'>Транзакция</a>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
