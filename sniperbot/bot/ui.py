"""Мелкие помощники для работы с сообщениями Telegram."""

from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)

# Telegram отвергает сообщения длиннее 4096 символов целиком, а не обрезает их,
# поэтому длинный текст режем сами — по строкам, чтобы не рвать HTML-разметку.
TELEGRAM_LIMIT = 4096


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Делит текст на части по границам строк, не превышая лимит."""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    chunk = ""
    for line in text.split("\n"):
        while len(line) > limit:            # одна строка длиннее лимита — режем жёстко
            if chunk:
                parts.append(chunk)
                chunk = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > limit:
            parts.append(chunk)
            chunk = line
        else:
            chunk = candidate
    if chunk:
        parts.append(chunk)
    return parts


async def safe_edit(
    event: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None, **kwargs
) -> None:
    """Редактирует сообщение; если нельзя — отправляет новое."""
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    message = event.message if isinstance(event, CallbackQuery) else event
    if message is None:
        return

    parts = split_message(text)
    try:
        await message.edit_text(parts[0], reply_markup=markup if len(parts) == 1 else None, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        try:
            await message.answer(parts[0], reply_markup=markup if len(parts) == 1 else None, **kwargs)
        except Exception as inner:  # noqa: BLE001
            log.debug("Не удалось отправить сообщение: %s", inner)
            return

    for index, part in enumerate(parts[1:], start=2):
        try:
            await message.answer(part, reply_markup=markup if index == len(parts) else None, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось отправить часть %s: %s", index, exc)


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None,
                **kwargs) -> Message:
    """Отправляет ответ, разбивая слишком длинный текст на несколько сообщений."""
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)

    parts = split_message(text)
    sent = None
    for index, part in enumerate(parts, start=1):
        sent = await message.answer(
            part, reply_markup=markup if index == len(parts) else None, **kwargs
        )
    return sent
