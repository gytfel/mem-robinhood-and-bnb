"""Реферальная программа и комиссии сервиса — глазами пользователя."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import User
from sniperbot.fees import (
    STATE_KEY,
    apply_fee_change,
    referral_link,
    referral_progress,
    status_for,
)
from sniperbot.utils.evm import is_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="referral")


@router.message(Command("ref", "invite", "referral"))
async def cmd_ref(message: Message, ctx: BotContext, user: User, chain: ChainConfig,
                  is_admin: bool = False) -> None:
    """Ссылка-приглашение, счётчик друзей и текущие комиссии."""
    policy = ctx.trader.fee_policy()
    async with session_scope() as session:
        referrals = await repo.referral_count(session, user.id)
        invited = await repo.referrals_of(session, user.id, limit=10)

    status = status_for(referrals=referrals, is_admin=is_admin,
                        exempt=bool(user.fee_exempt), policy=policy)

    me = await message.bot.get_me()
    link = referral_link(me.username, user.id)

    lines = ["👥 <b>Приглашайте друзей</b>\n"]
    if not policy.enabled:
        lines.append("Комиссии сервиса сейчас отключены — приглашать можно просто так.\n")
    else:
        lines.append(
            f"Комиссия за пополнение: <b>{status.deposit_pct:g}%</b>\n"
            f"Комиссия с прибыльных сделок: <b>{status.profit_pct:g}%</b> "
            "(с убыточных не берётся)\n"
        )

    progress = referral_progress(status)
    if progress:
        lines.append(progress)
    elif status.free_deposit:
        lines.append(f"✅ Пополнения без комиссии — {esc(status.reason)}.")

    if link:
        lines.append(f"\n<b>Ваша ссылка</b>\n<code>{link}</code>")
        lines.append("Друг должен открыть её и нажать «Старт» — тогда приглашение засчитается.")
    else:
        lines.append("\n⚠️ Не смог узнать имя бота — ссылка недоступна.")

    if invited:
        names = ", ".join(esc(item.username or str(item.id)) for item in invited)
        lines.append(f"\nПришли по вашей ссылке: {names}")

    if user.fees_paid_wei:
        lines.append(f"\nВсего удержано комиссий: "
                     f"{fmt_amount(from_wei(user.fees_paid_wei))} {chain.native_symbol}")
    await reply(message, "\n".join(lines))


@router.message(Command("fees"))
async def cmd_fees(message: Message, command: CommandObject, ctx: BotContext,
                   chain: ChainConfig, is_admin: bool = False) -> None:
    """Сколько собрано комиссий — команда владельца."""
    if not is_admin:
        await reply(message, "🔒 Команда только для администратора. Ваши комиссии: /ref")
        return

    changed, answer = apply_fee_change(ctx.fees, command.args or "", is_address=is_address)
    if changed:
        # Решение переживает перезапуск и важнее .env — как и у /access.
        async with session_scope() as session:
            await repo.set_state(session, STATE_KEY, ctx.fees.to_state())
    if answer:
        await reply(message, answer)
        return
    if (command.args or "").strip():
        await reply(message, _fees_usage())
        return

    policy = ctx.trader.fee_policy()
    async with session_scope() as session:
        total = await repo.fees_total(session)
        users = await repo.all_users(session, with_wallet=False)

    payers = sorted((item for item in users if item.fees_paid_wei),
                    key=lambda item: int(item.fees_paid_wei), reverse=True)

    lines = [
        "💼 <b>Комиссии сервиса</b>\n",
        f"Кошелёк сбора: <code>{esc(policy.wallet or 'не задан')}</code>",
        f"За пополнение: {policy.deposit_bps / 100:g}% · с прибыли: {policy.profit_bps / 100:g}%",
        f"Бесплатные пополнения после {policy.referrals_needed} приглашённых\n",
        f"Собрано всего: <b>{fmt_amount(from_wei(total))} {chain.native_symbol}</b>",
        f"Плательщиков: {len(payers)} из {len(users)}",
    ]
    if not policy.enabled:
        reason = ("выключены командой <code>/fees off</code>" if ctx.fees.off
                  else "не задан кошелёк сбора")
        lines.append(f"\n⚠️ <b>Комиссии не берутся</b> — {reason}.\n"
                     "Включить: <code>/fees wallet 0xВашАдрес</code>")
    for item in payers[:10]:
        lines.append(f"   · {esc(item.username or str(item.id))}: "
                     f"{fmt_amount(from_wei(item.fees_paid_wei))}")
    lines.append("\nОсвободить пользователя: <code>/exempt ID</code> · "
                 "вернуть комиссию: <code>/exempt ID off</code>")
    lines.append(_fees_usage())
    await reply(message, "\n".join(lines))


def _fees_usage() -> str:
    """Управление комиссиями прямо из бота — без правки .env и перезапуска."""
    return ("\n<b>Управление</b>\n"
            "<code>/fees on</code> · <code>/fees off</code> — включить и выключить\n"
            "<code>/fees wallet 0x…</code> — куда собирать\n"
            "<code>/fees deposit 2</code> — % с пополнения\n"
            "<code>/fees profit 5</code> — % с прибыли сделки\n"
            "<code>/fees refs 3</code> — сколько друзей снимают комиссию за пополнение")


@router.message(Command("exempt"))
async def cmd_exempt(message: Message, command, is_admin: bool = False) -> None:  # noqa: ANN001
    """Ручное освобождение от комиссий — команда владельца."""
    if not is_admin:
        await reply(message, "🔒 Команда только для администратора.")
        return

    parts = (command.args or "").split()
    if not parts or not parts[0].lstrip("-").isdigit():
        await reply(message, "Использование: <code>/exempt ID</code> — снять комиссии,\n"
                             "<code>/exempt ID off</code> — вернуть их.")
        return

    target = int(parts[0])
    turn_on = not (len(parts) > 1 and parts[1].lower() in {"off", "выкл", "нет", "0"})
    async with session_scope() as session:
        target_user = await repo.get_user(session, target)
        if target_user is None:
            await reply(message, f"Пользователь {target} не найден.")
            return
        target_user.fee_exempt = turn_on

    await reply(message, f"{'✅ Освобождён от комиссий' if turn_on else '↩️ Комиссии возвращены'}: "
                         f"<code>{target}</code>")
