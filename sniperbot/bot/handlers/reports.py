"""Отчёты: P&L, статистика потока токенов, оценка преимущества, чёрный список."""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from collections import Counter
from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import User
from sniperbot.utils.evm import extract_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="reports")


def _days_arg(args: str | None, default: int = 7) -> tuple[int, bool]:
    """Разбирает «[test] [дней]» из аргументов команды."""
    parts = (args or "").split()
    paper = bool(parts) and parts[0].lower() in {"test", "тест", "paper"}
    if paper:
        parts = parts[1:]
    days = int(parts[0]) if parts and parts[0].isdigit() else default
    return max(1, min(365, days)), paper


@router.message(Command("pnl"))
async def cmd_pnl(message: Message, command: CommandObject, ctx: BotContext, user: User) -> None:
    days, paper = _days_arg(command.args, default=7)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, since, paper=paper)
        open_positions = await repo.open_positions(session, user_id=user.id)

    title = "🧪 Бумажные сделки" if paper else "💰 Реальные сделки"
    if not positions:
        await reply(message, f"{title} за {days} дн.: сделок не было.")
        return

    spent = sum(p.native_spent_wei for p in positions)
    returned = sum(p.native_returned_wei for p in positions)
    net = returned - spent
    wins = [p for p in positions if p.native_returned_wei > p.native_spent_wei]
    best = max(positions, key=lambda p: p.native_returned_wei - p.native_spent_wei)
    worst = min(positions, key=lambda p: p.native_returned_wei - p.native_spent_wei)
    pct = (Decimal(net) / Decimal(spent) * 100) if spent else Decimal(0)
    icon = "🟢" if net >= 0 else "🔴"

    lines = [
        f"{title} за {days} дн.\n",
        f"Сделок: <b>{len(positions)}</b> · прибыльных: <b>{len(wins)}</b> "
        f"({len(wins) * 100 // len(positions)}%)",
        f"Вложено: {fmt_amount(from_wei(spent))} · возвращено: {fmt_amount(from_wei(returned))}",
        f"{icon} Итог: <b>{fmt_amount(from_wei(net))}</b> ({pct:+.1f}%)",
        f"Лучшая: {esc(best.token_symbol)} {fmt_amount(from_wei(best.native_returned_wei - best.native_spent_wei))}",
        f"Худшая: {esc(worst.token_symbol)} {fmt_amount(from_wei(worst.native_returned_wei - worst.native_spent_wei))}",
    ]
    if open_positions:
        lines.append(f"\nОткрыто сейчас: {len(open_positions)} (в расчёт не входят)")
    await reply(message, "\n".join(lines))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["закрыта", "сеть", "токен", "адрес", "площадка", "источник",
                     "вложено", "возвращено", "pnl", "pnl_%"])
    for position in positions:
        position_spent = from_wei(position.native_spent_wei)
        position_pnl = from_wei(position.native_returned_wei - position.native_spent_wei)
        writer.writerow([
            (position.closed_at or position.opened_at).strftime("%Y-%m-%d %H:%M"),
            position.chain, position.token_symbol, position.token_address,
            f"{position.dex_kind}{'/' + str(position.pool_fee) if position.pool_fee else ''}",
            "тест" if position.is_paper else position.source,
            f"{position_spent:.8f}", f"{from_wei(position.native_returned_wei):.8f}",
            f"{position_pnl:.8f}",
            f"{(position_pnl / position_spent * 100):.2f}" if position_spent else "",
        ])
    name = f"pnl-{'test-' if paper else ''}{dt.date.today()}.csv"
    await message.answer_document(
        BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), filename=name),
        caption=f"Сделки за {days} дн.",
    )


@router.message(Command("edge"))
async def cmd_edge(message: Message, command: CommandObject, user: User) -> None:
    days, paper = _days_arg(command.args, default=30)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, since, paper=paper)

    if len(positions) < 2:
        await reply(message, f"Мало данных: сделок за {days} дн. — {len(positions)}. "
                             "Оценка появится, когда наберётся хотя бы десяток.")
        return

    results = [(from_wei(p.native_returned_wei - p.native_spent_wei),
                from_wei(p.native_spent_wei)) for p in positions]
    wins = [pnl for pnl, _ in results if pnl > 0]
    losses = [pnl for pnl, _ in results if pnl <= 0]
    total = sum(pnl for pnl, _ in results)
    avg_win = sum(wins) / len(wins) if wins else Decimal(0)
    avg_loss = sum(losses) / len(losses) if losses else Decimal(0)
    winrate = Decimal(len(wins)) / Decimal(len(results)) * 100
    expectancy = total / len(results)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else None

    verdict = "🟢 похоже на преимущество" if expectancy > 0 else "🔴 стратегия в минусе"
    if len(results) < 20:
        verdict += " — но выборка мала, это может быть случайность"

    await reply(
        message,
        f"{'🧪 Бумажные' if paper else '💰 Реальные'} сделки за {days} дн.\n\n"
        f"Сделок: <b>{len(results)}</b>\n"
        f"Винрейт: <b>{winrate:.0f}%</b>\n"
        f"Средняя прибыльная: {fmt_amount(avg_win)}\n"
        f"Средний убыток: {fmt_amount(avg_loss)}\n"
        f"Ожидание на сделку: <b>{fmt_amount(expectancy)}</b>\n"
        + (f"Профит-фактор: <b>{profit_factor:.2f}</b>\n" if profit_factor else "")
        + f"\n<b>Вывод:</b> {verdict}",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject, ctx: BotContext, user: User) -> None:
    hours = int(command.args) if (command.args or "").strip().isdigit() else 24
    hours = max(1, min(720, hours))
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours)

    lines = [f"📈 <b>Поток токенов</b> за {hours} ч\n"]
    total = 0
    for chain_key in ctx.active_chain_keys:
        async with session_scope() as session:
            pairs = await repo.pairs_since(session, chain_key, since)
        if not pairs:
            lines.append(f"{esc(ctx.chain(chain_key).name)}: новых пулов нет")
            continue
        total += len(pairs)
        statuses = Counter(pair.status for pair in pairs)
        lines.append(
            f"<b>{esc(ctx.chain(chain_key).name)}</b>: найдено {len(pairs)}, "
            f"куплено {statuses.get('sniped', 0)}, отсеяно {statuses.get('rejected', 0)}"
        )
        reasons = Counter(
            (pair.reason or "без причины").split(";")[0].strip()[:60]
            for pair in pairs if pair.status == "rejected" and pair.reason
        )
        for reason, count in reasons.most_common(5):
            lines.append(f"   • {esc(reason)} — {count}")

    if not total:
        lines.append("\nПусто. Либо сеть тихая, либо сканер не видит фабрику — проверьте /health.")
    else:
        lines.append("\nСлишком строгие фильтры видно по частым причинам отказа: /config")
    await reply(message, "\n".join(lines))


@router.message(Command("blacklist"))
async def cmd_blacklist(message: Message, command: CommandObject, user: User, chain) -> None:
    parts = (command.args or "").split()
    action = parts[0].lower() if parts else "list"

    if action in {"add", "del", "remove"} and len(parts) > 1:
        token = extract_address(parts[1])
        if not token:
            await reply(message, "❌ Нужен адрес токена: <code>/blacklist add 0x…</code>")
            return
        async with session_scope() as session:
            if action == "add":
                await repo.add_flag(session, chain.key, token, "blacklist", user.id, "вручную")
                text = f"⛔️ Токен добавлен в чёрный список ({esc(chain.name)}):\n<code>{token}</code>"
            else:
                await repo.add_flag(session, chain.key, token, "whitelist", user.id, "снят из ЧС")
                text = f"✅ Токен убран из чёрного списка:\n<code>{token}</code>"
        await reply(message, text)
        return

    await reply(
        message,
        "⛔️ <b>Чёрный список</b>\n\n"
        "<code>/blacklist add 0xТокен</code> — не покупать этот токен\n"
        "<code>/blacklist del 0xТокен</code> — снять запрет\n\n"
        "Список действует в текущей сети и учитывается автоснайпом.",
    )


@router.message(Command("optimize"))
async def cmd_optimize(message: Message, command: CommandObject, user: User, chain) -> None:
    """Подбирает TP/SL, которые дали бы лучший результат на ваших же сделках."""
    days, paper = _days_arg(command.args, default=30)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, since, paper=paper)

    usable = [p for p in positions if p.entry_price and p.entry_price > 0 and p.peak_price]
    if len(usable) < 5:
        await reply(
            message,
            f"Для подбора нужно хотя бы 5 закрытых сделок с историей цены, есть {len(usable)}.\n"
            "Погоняйте бота в тестовом режиме (/dry) — данные накопятся быстрее и бесплатно.",
        )
        return

    trades = []
    for position in usable:
        peak = (position.peak_price / position.entry_price - 1) * 100
        spent = from_wei(position.native_spent_wei)
        final = ((from_wei(position.native_returned_wei) / spent - 1) * 100) if spent else Decimal(0)
        trades.append((peak, final))

    best = None
    for take_profit in (50, 75, 100, 150, 200, 300, 500):
        for stop_loss in (20, 30, 40, 50, 60, 70):
            total = sum(_simulate(peak, final, take_profit, stop_loss) for peak, final in trades)
            wins = sum(1 for peak, final in trades if _simulate(peak, final, take_profit, stop_loss) > 0)
            average = total / len(trades)
            if best is None or average > best[0]:
                best = (average, take_profit, stop_loss, wins)

    average, take_profit, stop_loss, wins = best
    current = sum(final for _peak, final in trades) / len(trades)
    await reply(
        message,
        f"🔧 <b>Подбор выходов</b> по {len(trades)} сделкам за {days} дн."
        + (" (бумажным)" if paper else "") + "\n\n"
        f"Сейчас средний результат: <b>{current:+.1f}%</b> на сделку\n"
        f"Лучшая пара из перебранных: <b>TP +{take_profit}% / SL −{stop_loss}%</b>\n"
        f"Дала бы <b>{average:+.1f}%</b> на сделку, прибыльных {wins} из {len(trades)}\n\n"
        f"Применить: <code>/set tp {take_profit}</code> и <code>/set sl {stop_loss}</code>\n\n"
        "<i>Это прикидка на прошлых данных: считается по записанному максимуму цены, "
        "без учёта проскальзывания и того, что рынок меняется. Не гарантия.</i>",
    )


def _simulate(peak: Decimal, final: Decimal, take_profit: int, stop_loss: int) -> Decimal:
    """Что дала бы сделка при заданных TP/SL: цель, стоп или фактический исход."""
    if peak >= take_profit:
        return Decimal(take_profit)
    if final <= -stop_loss:
        return Decimal(-stop_loss)
    return final
