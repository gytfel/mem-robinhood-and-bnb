"""Команды администратора."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position, SeenPair, User
from sniperbot.utils.fmt import esc

router = Router(name="admin")


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if not is_admin:
        return
    async with session_scope() as session:
        users = await session.scalar(select(func.count()).select_from(User))
        open_positions = await session.scalar(
            select(func.count()).select_from(Position).where(Position.status == "open")
        )
        pairs = await session.scalar(select(func.count()).select_from(SeenPair))

    lines = [
        "🛠 <b>Статистика</b>\n",
        f"Пользователей: <b>{users}</b>",
        f"Открытых позиций: <b>{open_positions}</b>",
        f"Просмотрено пар: <b>{pairs}</b>\n",
        "<b>Сети</b>",
    ]
    for key in ctx.registry.configs:
        config = ctx.chain(key)
        state = "включена" if config.enabled and config.configured else "выключена"
        lines.append(f"· {esc(config.name)}: {state}")
        if config.enabled and config.configured:
            try:
                block = await ctx.registry.get(key).block_number()
                lines.append(f"  блок {block}, RPC {esc(ctx.registry.get(key).rpc_url)}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  ⚠️ RPC недоступен: {esc(exc)}")
    await reply(message, "\n".join(lines))
