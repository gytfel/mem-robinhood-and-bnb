"""Точка входа: `python -m sniperbot [команда]`.

Без аргументов запускает бота — это то же самое, что `sniper run`.
"""

from __future__ import annotations

import sys

from sniperbot.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["run"]
    sys.exit(main(argv))
