"""Мелкие помощники для работы с сообщениями Telegram."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)


async def safe_edit(
    event: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs
) -> None:
    """Редактирует сообщение; если нельзя — отправляет новое."""
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    message = event.message if isinstance(event, CallbackQuery) else event
    if message is None:
        return
    try:
        await message.edit_text(text, reply_markup=markup, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        try:
            await message.answer(text, reply_markup=markup, **kwargs)
        except Exception as inner:  # noqa: BLE001
            log.debug("Не удалось отправить сообщение: %s", inner)


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs) -> Message:
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await message.answer(text, reply_markup=markup, **kwargs)
