"""Мидлвари: контроль доступа и загрузка пользователя."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from sniperbot.bot.context import BotContext
from sniperbot.bot.texts import NOT_ALLOWED
from sniperbot.db import repo
from sniperbot.db.base import session_scope

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Белый список пользователей (если задан ALLOWED_USER_IDS)."""

    def __init__(self, allowed: set[int], admins: set[int]) -> None:
        self.allowed = allowed
        self.admins = admins

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:  # noqa: ANN001
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        if self.allowed and tg_user.id not in self.allowed and tg_user.id not in self.admins:
            if isinstance(event, Message):
                await event.answer(NOT_ALLOWED)
            elif isinstance(event, CallbackQuery):
                await event.answer(NOT_ALLOWED, show_alert=True)
            return None
        data["is_admin"] = tg_user.id in self.admins
        return await handler(event, data)


class UserMiddleware(BaseMiddleware):
    """Создаёт пользователя и кошелёк, подставляет user/cfg/chain в хендлер."""

    def __init__(self, ctx: BotContext) -> None:
        self.ctx = ctx

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return await handler(event, data)

        try:
            chain_key = self.ctx.resolve_chain(None)
        except RuntimeError as exc:
            log.error("%s", exc)
            if isinstance(event, Message):
                await event.answer(f"⚠️ {exc}")
            return None

        async with session_scope() as session:
            user, created = await repo.get_or_create_user(
                session, tg_user.id, tg_user.username, default_chain=chain_key
            )
            if self.ctx.wallets.ensure_wallet(user):
                created = True
            user.active_chain = self.ctx.resolve_chain(user.active_chain)
            cfg = await repo.get_settings(session, user.id, user.active_chain)

        data["user"] = user
        data["cfg"] = cfg
        data["chain_key"] = user.active_chain
        data["chain"] = self.ctx.chain(user.active_chain)
        data["is_new_user"] = created
        return await handler(event, data)
