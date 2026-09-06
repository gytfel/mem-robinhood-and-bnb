"""Команды администратора: здоровье, расход ресурсов, пользователи, рассылка."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.chain.clients import ChainClient
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position, SeenPair, TradeLog
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="admin")


def _deny(is_admin: bool) -> bool:
    return not is_admin


@router.message(Command("stats_admin", "adminstats"))
@router.message(Command("health"))
async def cmd_health(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    lines = ["🩺 <b>Здоровье бота</b>\n", "<b>Сканеры</b>"]
    statuses = ctx.engine.status()
    if not statuses:
        lines.append("  ⛔️ ни один сканер не запущен — проверьте настройки сетей")
    for status in statuses:
        icon = "✅" if status["running"] else "⛔️"
        error = f" — {esc(status['error'])}" if status["error"] else ""
        lines.append(f"  {icon} {esc(status['name'])}{error}")

    lines.append("\n<b>Сети</b>")
    for key in ctx.active_chain_keys:
        chain = ctx.chain(key)
        client = ctx.registry.get(key)
        started = time.perf_counter()
        try:
            block = await client.block_number()
            latency = (time.perf_counter() - started) * 1000
            lines.append(f"  ✅ {esc(chain.name)}: блок {block}, {latency:.0f} мс")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  ⛔️ {esc(chain.name)}: {esc(str(exc)[:80])}")
            continue
        async with session_scope() as session:
            recent = await repo.recent_pairs(session, key, limit=1)
        if recent:
            age = dt.datetime.now(dt.UTC) - _aware(recent[0].created_at)
            lines.append(f"     последний пул: {int(age.total_seconds() // 60)} мин назад")
        else:
            lines.append("     новых пулов ещё не видел")

    async with session_scope() as session:
        open_positions = await session.scalar(
            select(func.count()).select_from(Position).where(Position.status == "open")
        )
    lines.append(f"\nОткрытых позиций у всех: <b>{open_positions}</b>")
    await reply(message, "\n".join(lines))


@router.message(Command("usage"))
async def cmd_usage(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    lines = ["📊 <b>Расход ресурсов</b>\n", "<b>Запросы к RPC</b> (с момента запуска)"]
    for key in ctx.active_chain_keys:
        client = ctx.registry.get(key)
        lines.append(f"  {esc(ctx.chain(key).name)}: {client.requests} запросов, "
                     f"сбоев {client.failures}, активный узел {esc(client.rpc_url)}")

    async with session_scope() as session:
        users = await repo.user_count(session)
        positions = await session.scalar(select(func.count()).select_from(Position))
        pairs = await session.scalar(select(func.count()).select_from(SeenPair))
        trades = await session.scalar(select(func.count()).select_from(TradeLog))

    lines.append(f"\n<b>База</b>\n  пользователей {users} · позиций {positions} · "
                 f"пулов {pairs} · сделок {trades}")
    db_path = _database_path(ctx.settings.resolved_database_url)
    if db_path and db_path.exists():
        size = db_path.stat().st_size / 1024 / 1024
        lines.append(f"  файл: {esc(str(db_path))} — {size:.1f} МБ")
    await reply(message, "\n".join(lines))


@router.message(Command("latency"))
async def cmd_latency(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    status = await reply(message, "⏱ Замеряю задержки эндпоинтов…")
    lines = ["⏱ <b>Задержки RPC</b>"]
    for key in ctx.active_chain_keys:
        chain: ChainConfig = ctx.chain(key)
        lines.append(f"\n<b>{esc(chain.name)}</b>")
        for url in chain.rpc_urls:
            single = ChainClient(_single_rpc(chain, url))
            started = time.perf_counter()
            try:
                await single.block_number()
                lines.append(f"  ✅ {esc(url)} — {(time.perf_counter() - started) * 1000:.0f} мс")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  ⛔️ {esc(url)} — {esc(str(exc)[:60])}")
            finally:
                await single.close()
    lines.append("\nБот сам переключается на живой узел; медленные лучше убрать из .env.")
    await status.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("users"))
async def cmd_users(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    async with session_scope() as session:
        users = await repo.list_users(session, limit=40)
        total = await repo.user_count(session)
        rows = []
        for user in users:
            open_count = await session.scalar(
                select(func.count()).select_from(Position).where(
                    Position.user_id == user.id, Position.status == "open")
            )
            rows.append((user, int(open_count or 0)))

    lines = [f"👥 <b>Пользователи</b>: {total}\n"]
    for user, open_count in rows:
        name = f"@{user.username}" if user.username else str(user.id)
        flags = []
        if user.is_blocked:
            flags.append("🚫")
        if user.dry_run:
            flags.append("🧪")
        lines.append(f"{' '.join(flags)} <code>{user.id}</code> {esc(name)} · позиций {open_count}")
    lines.append("\nКарточка: <code>/userinfo ID</code>")
    await reply(message, "\n".join(lines))


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, command: CommandObject, ctx: BotContext,
                       is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await reply(message, "Использование: <code>/userinfo ID</code>")
        return

    user_id = int(raw)
    async with session_scope() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            await reply(message, "Пользователь не найден.")
            return
        open_positions = await repo.open_positions(session, user_id=user_id)
        spent, returned = await repo.total_pnl(session, user_id)

    lines = [
        f"👤 <b>{esc('@' + user.username if user.username else user_id)}</b>",
        f"ID: <code>{user.id}</code>",
        f"Кошелёк: <code>{user.wallet_address}</code>",
        f"Сеть: {esc(user.active_chain)} · режим: {'🧪 тест' if user.dry_run else '💰 боевой'}",
        f"Статус: {'🚫 заблокирован' if user.is_blocked else '✅ активен'}",
        f"Открытых позиций: {len(open_positions)}",
        f"Итог по закрытым: {fmt_amount(from_wei(returned - spent))}",
        f"Создан: {user.created_at:%d.%m.%Y}",
    ]
    for key in ctx.active_chain_keys:
        try:
            balance = await ctx.registry.get(key).native_balance(user.wallet_address)
            lines.append(f"{esc(ctx.chain(key).name)}: {fmt_amount(from_wei(balance))} "
                         f"{ctx.chain(key).native_symbol}")
        except Exception:  # noqa: BLE001
            continue
    await reply(message, "\n".join(lines))


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, is_admin: bool = False) -> None:
    await _set_block(message, command, is_admin, True)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, is_admin: bool = False) -> None:
    await _set_block(message, command, is_admin, False)


async def _set_block(message: Message, command: CommandObject, is_admin: bool, blocked: bool) -> None:
    if _deny(is_admin):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await reply(message, f"Использование: <code>/{'ban' if blocked else 'unban'} ID</code>")
        return
    async with session_scope() as session:
        found = await repo.set_blocked(session, int(raw), blocked)
    if not found:
        await reply(message, "Пользователь не найден.")
        return
    await reply(message, f"{'🚫 Заблокирован' if blocked else '✅ Разблокирован'}: <code>{raw}</code>"
                         + ("\nАвтоснайп для него остановлен, позиции остаются." if blocked else ""))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, ctx: BotContext,
                        is_admin: bool = False) -> None:
    if _deny(is_admin):
        return
    text = (command.args or "").strip()
    if not text:
        await reply(message, "Использование: <code>/broadcast текст сообщения</code>")
        return

    async with session_scope() as session:
        users = await repo.all_users(session, with_wallet=False)

    sent = 0
    status = await reply(message, f"📢 Отправляю {len(users)} пользователям…")
    for user in users:
        await ctx.notifier.send(user.id, f"📢 <b>Сообщение от администратора</b>\n\n{esc(text)}")
        sent += 1
        await asyncio.sleep(0.05)      # ~20 сообщений в секунду — лимит Telegram
    await status.edit_text(f"📢 Отправлено: {sent} из {len(users)}", parse_mode="HTML")


def _single_rpc(chain: ChainConfig, url: str) -> ChainConfig:
    """Копия конфига сети с единственным RPC — для честного замера."""
    from dataclasses import replace

    return replace(chain, rpc_urls=[url])


def _database_path(url: str) -> Path | None:
    marker = "sqlite+aiosqlite:///"
    if not url.startswith(marker):
        return None
    raw = url[len(marker):]
    return None if raw == ":memory:" else Path(raw)


def _aware(value):  # noqa: ANN001
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
