"""Отправка уведомлений пользователям (обёртка над Telegram Bot API)."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Минимальный интерфейс, который нужен фоновым задачам."""

    async def send(self, user_id: int, text: str, **kwargs) -> None: ...


class TelegramNotifier:
    """Шлёт сообщения через aiogram, гасит ошибки доставки."""

    def __init__(self, bot) -> None:  # noqa: ANN001 - aiogram.Bot
        self.bot = bot

    async def send(self, user_id: int, text: str, **kwargs) -> None:
        kwargs.setdefault("parse_mode", "HTML")
        kwargs.setdefault("disable_web_page_preview", True)
        try:
            await self.bot.send_message(user_id, text, **kwargs)
        except Exception as exc:  # noqa: BLE001 - юзер мог заблокировать бота
            log.debug("Не смог отправить сообщение %s: %s", user_id, exc)


class NullNotifier:
    """Заглушка для тестов и CLI-режима."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send(self, user_id: int, text: str, **kwargs) -> None:
        self.messages.append((user_id, text))
        await asyncio.sleep(0)
