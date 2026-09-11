"""Мидлвари: контроль доступа и загрузка пользователя."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from sniperbot.access import AccessPolicy
from sniperbot.bot.context import BotContext
from sniperbot.bot.texts import BANNED, NOT_ALLOWED
from sniperbot.db import repo
from sniperbot.db.base import session_scope

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Пускать или нет. Само правило живёт в :class:`AccessPolicy`.

    Политика — общий изменяемый объект: команда /access меняет её на ходу, и
    мидлварь видит новое правило со следующего же сообщения.
    """

    def __init__(self, policy: AccessPolicy) -> None:
        self.policy = policy

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:  # noqa: ANN001
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None:
            return await handler(event, data)
        if not self.policy.allows(tg_user.id):
            await refuse(event, NOT_ALLOWED)
            return None
        data["is_admin"] = tg_user.id in self.policy.admins
        return await handler(event, data)


async def refuse(event: TelegramObject, text: str) -> None:
    """Отказ понятным сообщением, а не молчанием."""
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)


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
            blocked = bool(user.is_blocked)

        # Бан проверяется здесь, а не в AccessMiddleware: там нет обращения к
        # базе, и без этой проверки /ban оставался бы пометкой в карточке, а
        # забаненный продолжал бы пользоваться ботом.
        if blocked and not data.get("is_admin"):
            await refuse(event, BANNED)
            return None

        data["user"] = user
        data["cfg"] = cfg
        data["chain_key"] = user.active_chain
        data["chain"] = self.ctx.chain(user.active_chain)
        data["is_new_user"] = created
        return await handler(event, data)
