"""Отчёты: P&L, статистика потока токенов, оценка преимущества, чёрный список."""

from __future__ import annotations

import datetime as dt
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
from sniperbot.pairstats import (
    MIN_SAMPLE,
    render_hours,
    render_outcomes,
    render_winrate,
    round_trip_cost,
    split,
    to_outcomes,
)
from sniperbot.reports import (
    period_breakdown,
    period_label,
    render_anatomy,
    render_report,
    render_summary,
    summarize,
    to_rows,
    trades_csv,
)
from sniperbot.utils.evm import extract_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="reports")


def _days_arg(args: str | None) -> tuple[int | None, bool]:
    """Разбирает «[test] [дней]». Без числа — за всё время (None)."""
    parts = (args or "").split()
    paper = bool(parts) and parts[0].lower() in {"test", "тест", "paper"}
    if paper:
        parts = parts[1:]
    if parts and parts[0].isdigit():
        return max(1, min(3650, int(parts[0]))), paper
    return None, paper


def _since(days: int | None) -> dt.datetime | None:
    return None if days is None else dt.datetime.now(dt.UTC) - dt.timedelta(days=days)


@router.message(Command("pnl"))
async def cmd_pnl(message: Message, command: CommandObject, ctx: BotContext, user: User,
                  chain) -> None:
    """Отчёт по одному режиму: боевому или тестовому."""
    days, paper = _days_arg(command.args)

    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, _since(days), paper=paper)
        open_positions = await repo.open_positions(session, user_id=user.id)

    label = "🧪 Тестовые сделки" if paper else "💰 Реальные сделки"
    summary = summarize(positions, label, chain.native_decimals)
    period = period_label(days, summary.rows)
    if not summary.count:
        await reply(message, f"{label} {period}: сделок не было.")
        return

    text = f"🧾 <b>Отчёт</b> {period}\n\n" + render_summary(summary, chain.native_symbol)
    anatomy = render_anatomy(summary, chain.native_symbol)
    if anatomy:
        text += "\n\n" + anatomy
    windows = period_breakdown(summary.rows, chain.native_symbol)
    if windows and days is None:
        text += "\n\n📅 <b>По периодам</b>\n" + "\n".join(windows)
    if open_positions:
        text += f"\n\nОткрытых позиций сейчас: <b>{len(open_positions)}</b> (/positions)"
    await reply(message, text)
    await _send_file(message, summary.rows, [], f"pnl-{'test-' if paper else ''}{dt.date.today()}",
                     f"Сделки {period}")


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject, ctx: BotContext, user: User,
                     chain) -> None:
    """Сводный отчёт: боевые и тестовые сделки рядом, плюс файл со всеми."""
    days, _ = _days_arg(command.args)
    since = _since(days)

    async with session_scope() as session:
        real_positions = await repo.closed_between(session, user.id, since, paper=False)
        paper_positions = await repo.closed_between(session, user.id, since, paper=True)
        open_positions = await repo.open_positions(session, user_id=user.id)

    real = summarize(real_positions, "💰 Боевой режим", chain.native_decimals)
    paper = summarize(paper_positions, "🧪 Тестовый режим", chain.native_decimals)

    if not real.count and not paper.count:
        await reply(
            message,
            f"🧾 Отчёт {period_label(days)}: закрытых сделок нет.\n\n"
            "Наберите статистику бесплатно: /dry включает тестовый режим, "
            "сделки считаются по реальным котировкам без трат.",
        )
        return

    await reply(message, render_report(real, paper, days, chain.native_symbol, len(open_positions)))
    await _send_file(
        message,
        [*real.rows, *paper.rows],
        to_rows(open_positions, chain.native_decimals),
        f"сделки-{dt.date.today()}",
        f"Все сделки {period_label(days, [*real.rows, *paper.rows])} + открытые позиции",
    )


async def _send_file(message: Message, rows, open_rows, name: str, caption: str) -> None:
    """CSV с разделителем «;» и запятой в числах — открывается Excel как есть."""
    content = trades_csv(rows, open_rows)
    await message.answer_document(
        BufferedInputFile(content.encode("utf-8-sig"), filename=f"{name}.csv"),
        caption=caption,
    )


@router.message(Command("edge"))
async def cmd_edge(message: Message, command: CommandObject, user: User) -> None:
    days, paper = _days_arg(command.args)
    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, _since(days), paper=paper)

    if len(positions) < 2:
        await reply(message, f"Мало данных: сделок {period_label(days)} — {len(positions)}. "
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
        f"{'🧪 Бумажные' if paper else '💰 Реальные'} сделки "
        f"{period_label(days, to_rows(positions))}\n\n"
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
    """Поток токенов и — главное — правильно ли фильтры его режут."""
    hours = int(command.args) if (command.args or "").strip().isdigit() else None
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours) if hours else None
    window = f"за {hours} ч" if hours else "за всё время"

    lines = [f"📈 <b>Поток токенов</b> {window}\n"]
    total = 0
    outcome_blocks: list[str] = []
    for chain_key in ctx.active_chain_keys:
        chain = ctx.chain(chain_key)
        async with session_scope() as session:
            pairs = await repo.pairs_since(session, chain_key, since)
            counts = await repo.pair_status_counts(session, chain_key, since)
            tracked = await repo.outcome_pairs(session, chain_key, since)
        if not pairs:
            lines.append(f"{esc(chain.name)}: новых пулов нет")
            continue
        total += len(pairs)

        checked = [pair for pair in pairs if pair.analysis_ms > 0]
        average = sum(pair.analysis_ms for pair in checked) / len(checked) / 1000 if checked else 0
        lines.append(
            f"<b>{esc(chain.name)}</b>: найдено {len(pairs)}\n"
            f"   ✅ куплено {counts.get('sniped', 0)} · "
            f"⏳ ждут ликвидность {counts.get('waiting', 0)} · "
            f"⛔️ отсеяно {counts.get('rejected', 0)}"
            + (f"\n   ⏱ проверка токена: {average:.1f} с в среднем" if checked else "")
        )
        reasons = Counter(
            (pair.reason or "без причины").split(";")[0].strip()[:60]
            for pair in pairs if pair.status == "rejected" and pair.reason
        )
        for reason, count in reasons.most_common(5):
            lines.append(f"   • {esc(reason)} — {count}")

        outcomes = to_outcomes(tracked)
        if outcomes:
            outcome_blocks.append(render_outcomes(
                outcomes, seen=len(pairs), bought=counts.get("sniped", 0),
                window=f"{window} · {chain.name}",
            ))
            outcome_blocks.append(render_hours(outcomes, int(user.tz_offset or 0)))

    if not total:
        lines.append("\nПусто. Либо сеть тихая, либо сканер не видит фабрику — проверьте /health.")
    else:
        lines.append("\n<i>«Ждут ликвидность» — пары созданы, но денег в пул ещё не залили; "
                     "бот вернётся к ним сам.</i>\nСузить период: <code>/stats 24</code>")

    costs = await _costs_block(ctx, user)
    if costs:
        lines.append(costs)

    if outcome_blocks:
        lines.append("\n" + "\n\n".join(outcome_blocks))
    elif total:
        lines.append(
            "\n🔬 <b>Качество фильтров</b>: данных пока нет.\n"
            "Бот следит за ценой всех найденных пулов и через несколько часов покажет, "
            "что он отсеял — мусор или будущие иксы. Наблюдение работает при включённом "
            "автоснайпе (/on)."
        )
    await reply(message, "\n".join(lines))


async def _costs_block(ctx: BotContext, user: User) -> str:
    """Во что обходится круг «купил-продал» при текущей цене газа."""
    chain_key = ctx.resolve_chain(user.active_chain)
    chain = ctx.chain(chain_key)
    async with session_scope() as session:
        gas = await repo.gas_by_kind(session, user.id, chain_key)
        cfg = await repo.get_settings(session, user.id, chain_key)
    round_trip = gas.get("buy", 0) + gas.get("sell", 0)
    if not round_trip:
        return ""
    try:
        fees = await ctx.registry.get(chain_key).gas_fees()
    except Exception as exc:  # noqa: BLE001 - без цены газа просто не показываем блок
        log.debug("Цена газа для отчёта недоступна: %s", exc)
        return ""
    price = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0)
    if not price:
        return ""

    cost, share = round_trip_cost(round_trip, price, Decimal(str(cfg.buy_amount)),
                                  chain.native_decimals)
    text = (f"\n<b>Издержки</b>\n"
            f"{fmt_amount(cost, 5)} {chain.native_symbol} за круг при входе "
            f"{fmt_amount(cfg.buy_amount)} = <b>{share:.1f}%</b>")
    if share >= 5:
        text += ("\n⚠️ Газ съедает больше 5% входа — увеличьте сумму покупки "
                 "(<code>/set buy</code>) или ждите более дешёвого газа.")
    return text


@router.message(Command("winrate"))
async def cmd_winrate(message: Message, command: CommandObject, ctx: BotContext,
                      user: User) -> None:
    """Какая пара TP/SL даёт нужную долю плюсовых сделок — и сколько это стоит."""
    parts = (command.args or "").split()
    target = Decimal(parts[0]) if parts and parts[0].isdigit() else Decimal(45)
    target = max(Decimal(5), min(Decimal(95), target))

    chain_key = ctx.resolve_chain(user.active_chain)
    async with session_scope() as session:
        tracked = await repo.outcome_pairs(session, chain_key)
        cfg = await repo.get_settings(session, user.id, chain_key)

    cost = Decimal(parts[1]) if len(parts) > 1 and parts[1].isdigit() else await _round_trip_pct(
        ctx, user, chain_key, cfg)

    outcomes = to_outcomes(tracked)
    passed, _ = split(outcomes)
    # Считаем по тем пулам, что проходят фильтры: именно их бот и покупает.
    rows, scope = (passed.rows, "прошедшим фильтры")
    if len(rows) < MIN_SAMPLE:
        rows, scope = (outcomes, "всем найденным (прошедших фильтры пока мало)")

    await reply(message, render_winrate(rows, target, cost, scope))


async def _round_trip_pct(ctx: BotContext, user: User, chain_key: str, cfg) -> Decimal:
    """Издержки круга в процентах от входа: газ по факту плюс комиссии DEX.

    Налоги токенов сюда не входят — они у каждого свои; поэтому это нижняя
    оценка, и пользователь может задать свою: /winrate 45 10
    """
    dex_fee = Decimal("0.6")          # 0.3% на вход и столько же на выход
    async with session_scope() as session:
        gas = await repo.gas_by_kind(session, user.id, chain_key)
    round_trip = gas.get("buy", 0) + gas.get("sell", 0)
    if not round_trip:
        return dex_fee + Decimal(4)   # газ ещё не измерен — берём осторожную прикидку
    try:
        fees = await ctx.registry.get(chain_key).gas_fees()
    except Exception as exc:  # noqa: BLE001
        log.debug("Цена газа недоступна: %s", exc)
        return dex_fee + Decimal(4)
    price = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0)
    if not price:
        return dex_fee + Decimal(4)
    _, share = round_trip_cost(round_trip, price, Decimal(str(cfg.buy_amount)),
                               ctx.chain(chain_key).native_decimals)
    return dex_fee + share


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
    days, paper = _days_arg(command.args)
    async with session_scope() as session:
        positions = await repo.closed_between(session, user.id, _since(days), paper=paper)

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
        f"🔧 <b>Подбор выходов</b> по {len(trades)} сделкам {period_label(days, to_rows(usable))}"
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
