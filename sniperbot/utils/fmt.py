"""Форматирование чисел и текста для сообщений Telegram."""

from __future__ import annotations

import html
from decimal import ROUND_DOWN, Decimal, InvalidOperation

DEC_ZERO = Decimal(0)


def to_wei(amount: Decimal | str | float, decimals: int = 18) -> int:
    return int(Decimal(str(amount)) * (Decimal(10) ** decimals))


def from_wei(amount: int, decimals: int = 18) -> Decimal:
    if decimals == 0:
        return Decimal(amount)
    return Decimal(amount) / (Decimal(10) ** decimals)


def fmt_amount(value: Decimal | int | float, digits: int = 6) -> str:
    """Компактное представление количества: убирает хвостовые нули."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if dec == 0:
        return "0"
    abs_dec = abs(dec)
    if abs_dec >= 1000:
        digits = min(digits, 2)
    elif abs_dec < Decimal("0.000001"):
        return f"{dec:.3e}"
    quant = Decimal(1).scaleb(-digits)
    text = str(dec.quantize(quant, rounding=ROUND_DOWN).normalize())
    return text if text not in {"-0", "0E-8"} else "0"


def fmt_native(value: Decimal | int | float, symbol: str, digits: int = 6) -> str:
    return f"{fmt_amount(value, digits)} {symbol}"


def fmt_pct(value: Decimal | float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    dec = Decimal(str(value))
    sign = "+" if dec > 0 else ""
    return f"{sign}{dec.quantize(Decimal(1).scaleb(-digits))}%"


def fmt_usd(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"${fmt_amount(value, 2)}"


def short_addr(address: str, head: int = 6, tail: int = 4) -> str:
    if not address or len(address) <= head + tail + 2:
        return address or "—"
    return f"{address[:head]}…{address[-tail:]}"


def esc(text: object) -> str:
    """Экранирование для parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def parse_decimal(raw: str) -> Decimal | None:
    """Разбор пользовательского ввода: '0,05' и '0.05' одинаково валидны."""
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", ".").replace(" ", "").replace("_", "")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return value


def progress_bar(fraction: float, width: int = 10) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "█" * filled + "░" * (width - filled)
