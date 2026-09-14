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
from sniperbot.bot.keyboards import (
    MenuCB,
    WalletCB,
    back_button,
    cancel_kb,
    confirm_export,
    confirm_withdraw_kb,
    wallet_menu,
)
from sniperbot.bot.texts import EXPORT_WARNING, WALLET_HINT
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.bot.views import render_wallet
from sniperbot.chain.wallet import WalletError
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.utils.evm import extract_address, has_code, to_checksum
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, parse_decimal, to_wei
from sniperbot.withdrawals import Destination, address_problem, destination_warning

log = logging.getLogger(__name__)

router = Router(name="wallet")


class WithdrawStates(StatesGroup):
    address = State()
    amount = State()
    confirm = State()


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
        f"📤 <b>Вывод {chain.native_symbol}</b> — сеть <b>{esc(chain.name)}</b>\n"
        f"Монеты придут только в этой сети: кошелёк получателя должен её "
        f"поддерживать, биржи её обычно не принимают.\n\n"
        "Пришлите адрес получателя (0x…):",
        cancel_kb("wallet"),
    )
    await callback.answer()


@router.message(WithdrawStates.address)
async def withdraw_address(message: Message, state: FSMContext, chain: ChainConfig) -> None:
    raw = (message.text or "").strip()
    problem = address_problem(extract_address(raw) or raw)
    if problem:
        await reply(message, f"❌ {problem}", cancel_kb("wallet"))
        return
    address = extract_address(raw) or raw
    await state.update_data(address=address)
    await state.set_state(WithdrawStates.amount)
    await reply(
        message,
        f"Сколько {chain.native_symbol} вывести на\n<code>{address}</code>?\n"
        f"Сеть: <b>{esc(chain.name)}</b> — монеты придут только в ней.\n\n"
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
            # Чаще всего сюда прилетает адрес: человек прислал всё одной строкой.
            hint = ("Адрес уже принят — сейчас нужна только сумма.\n"
                    if extract_address(message.text or "") else "")
            await reply(message, f"❌ Не понял сумму.\n{hint}"
                                 f"Пришлите число (например <code>0.05</code>) или слово «всё».",
                        cancel_kb("wallet"))
            return
    await _guarded_withdraw(message, state, ctx, user, cfg, chain, address, amount)


async def _guarded_withdraw(message: Message, state: FSMContext, ctx: BotContext, user: User,
                            cfg: ChainSettings, chain: ChainConfig, address: str,
                            amount: Decimal | None) -> None:
    """Спрашивает подтверждение, если адрес выглядит чужим для этой сети."""
    async with session_scope() as session:
        known = await repo.withdrawn_before(session, user.id, chain.key, address)

    warning = "" if known else destination_warning(await _inspect(ctx, chain, address), chain.name)
    if not warning:
        await state.clear()
        await _do_withdraw(message, ctx, user, cfg, chain, address, amount)
        return

    await state.update_data(address=address, amount=str(amount) if amount is not None else "")
    await state.set_state(WithdrawStates.confirm)
    await reply(
        message,
        f"⚠️ <b>Проверьте получателя</b>\n\n{warning}\n\n"
        f"Отправить {'всё' if amount is None else fmt_amount(amount)} "
        f"{chain.native_symbol} на\n<code>{address}</code>?",
        confirm_withdraw_kb(),
    )


@router.callback_query(WalletCB.filter(F.action == "withdraw_confirm"))
async def cb_withdraw_confirm(callback: CallbackQuery, state: FSMContext, ctx: BotContext,
                              user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    data = await state.get_data()
    address = data.get("address")
    if not address:
        await callback.answer("Нечего подтверждать", show_alert=True)
        return
    raw = data.get("amount") or ""
    await state.clear()
    await callback.answer("Отправляю…")
    await _do_withdraw(callback.message, ctx, user, cfg, chain, address,
                       parse_decimal(raw) if raw else None)


async def _inspect(ctx: BotContext, chain: ChainConfig, address: str) -> Destination:
    """Что говорит сеть про адрес получателя."""
    client = ctx.registry.get(chain.key)
    checksummed = to_checksum(address)
    try:
        code = await client.run(lambda w3: w3.eth.get_code(checksummed))
        nonce = await client.transaction_count(checksummed, "latest")
        balance = await client.native_balance(checksummed)
    except Exception as exc:  # noqa: BLE001 - нода молчит: не пугаем зря и не мешаем
        log.info("Не смог проверить адрес получателя %s: %s", address, exc)
        return Destination(nonce=1)
    return Destination(has_code=has_code(code), nonce=int(nonce), balance=int(balance))


@router.message(Command("withdraw"))
async def cmd_withdraw(
    message: Message, command: CommandObject, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig, state: FSMContext,
) -> None:
    args = (command.args or "").split()
    if not args:
        await state.set_state(WithdrawStates.address)
        await reply(
            message,
            f"📤 <b>Вывод {chain.native_symbol}</b> — сеть <b>{esc(chain.name)}</b>\n"
            f"Монеты придут только в этой сети: кошелёк получателя должен её "
            f"поддерживать, биржи её обычно не принимают.\n\n"
            "Пришлите адрес получателя (0x…):",
            cancel_kb("wallet"),
        )
        return
    problem = address_problem(extract_address(args[0]) or args[0])
    if problem:
        await reply(message, f"❌ {problem}")
        return
    address = extract_address(args[0]) or args[0]
    amount = parse_decimal(args[1]) if len(args) > 1 else None
    if len(args) > 1 and amount is None:
        await reply(message, "❌ Некорректная сумма.")
        return
    await _guarded_withdraw(message, state, ctx, user, cfg, chain, address, amount)


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
            amount_wei=actual, tx_hash=sent.tx_hash, address=address,
        )
    await status.edit_text(
        f"✅ Отправлено <b>{fmt_amount(from_wei(actual, chain.native_decimals))} {chain.native_symbol}</b>\n"
        f"на <code>{address}</code>\n"
        f"🌐 Сеть: <b>{esc(chain.name)}</b>"
        + (f" (chain id {chain.chain_id})" if chain.chain_id else "") + "\n\n"
        # Деньги «пропадают» почти всегда здесь: адрес один и тот же во всех
        # EVM-сетях, а монеты лежат только в той, где прошла транзакция.
        f"<i>Монеты видны только в сети {esc(chain.name)}. В кошельке должна быть "
        f"добавлена именно она — в другой сети по тому же адресу будет пусто. "
        f"Биржи эту сеть обычно не принимают: вывод на биржевой адрес пропадёт.</i>\n\n"
        f"<a href='{chain.tx_url(sent.tx_hash)}'>Транзакция</a>",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
