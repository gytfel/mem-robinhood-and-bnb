"""Настройка логирования."""

from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("aiogram.event", "aiohttp.access", "web3.providers", "web3.manager", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
