"""Роутеры Telegram-хендлеров в порядке подключения."""

from aiogram import Router

from sniperbot.bot.handlers import (
    admin,
    common,
    control,
    lab,
    positions,
    reports,
    settings,
    trade,
    wallet,
)


def build_router() -> Router:
    """Собирает общий роутер. Порядок важен: trade ловит адреса токенов последним."""
    root = Router(name="root")
    root.include_router(common.router)
    root.include_router(control.router)
    root.include_router(wallet.router)
    root.include_router(positions.router)
    root.include_router(reports.router)
    root.include_router(lab.router)
    root.include_router(settings.router)
    root.include_router(admin.router)
    root.include_router(trade.router)
    return root
