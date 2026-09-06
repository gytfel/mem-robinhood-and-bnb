"""Точка входа: python -m sniperbot"""

from __future__ import annotations

import asyncio
import logging

from sniperbot.bot.app import run_bot
from sniperbot.config import get_settings
from sniperbot.logging_setup import setup_logging

log = logging.getLogger("sniperbot")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        asyncio.run(run_bot())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code not in (None, 0):
            log.error("%s", exc)
            raise
        log.info("Бот остановлен")


if __name__ == "__main__":
    main()
