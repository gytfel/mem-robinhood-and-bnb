"""Сборка отчётов по сделкам: сводка, списки и выгрузка в файл.

Модуль намеренно не знает про Telegram и про базу: на вход — позиции, на выход —
готовый текст и CSV. Так расчёты можно проверить тестами.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from sniperbot.db.models import Position
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

SOURCE_TITLES = {
    "auto": "снайп новых пар",
    "momentum": "перехват разгона",
    "manual": "покупки вручную",
}

EXIT_TITLES = {
    "take_profit": "тейк-профит",
    "ladder": "ступень фиксации",
    "stop_loss": "стоп-лосс",
    "breakeven": "выход в безубыток",
    "trailing": "трейлинг-стоп",
    "rug": "слив ликвидности",
    "dead": "позиция не росла",
    "panic": "аварийная продажа",
    "manual": "продали вручную",
    "": "неизвестно",
}


@dataclass(slots=True)
class TradeRow:
    """Одна сделка в понятном для отчёта виде."""

    position: Position
    spent: Decimal
    returned: Decimal

    @property
    def pnl(self) -> Decimal:
        return self.returned - self.spent

    @property
    def pnl_pct(self) -> Decimal:
        return (self.pnl / self.spent * 100) if self.spent > 0 else Decimal(0)

    @property
    def profitable(self) -> bool:
        return self.pnl > 0

    @property
    def exit_title(self) -> str:
        return EXIT_TITLES.get(self.position.exit_reason or "", self.position.exit_reason or "—")


@dataclass(slots=True)
class Summary:
    """Итоги по одному режиму (боевому или тестовому)."""

    label: str
    rows: list[TradeRow] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def wins(self) -> list[TradeRow]:
        return sorted((row for row in self.rows if row.profitable),
                      key=lambda row: row.pnl, reverse=True)

    @property
    def losses(self) -> list[TradeRow]:
        return sorted((row for row in self.rows if not row.profitable), key=lambda row: row.pnl)

    @property
    def spent(self) -> Decimal:
        return sum((row.spent for row in self.rows), Decimal(0))

    @property
    def returned(self) -> Decimal:
        return sum((row.returned for row in self.rows), Decimal(0))

    @property
    def pnl(self) -> Decimal:
        return self.returned - self.spent

    @property
    def pnl_pct(self) -> Decimal:
        return (self.pnl / self.spent * 100) if self.spent > 0 else Decimal(0)

    @property
    def winrate(self) -> int:
        return round(len(self.wins) * 100 / self.count) if self.count else 0

    @property
    def average(self) -> Decimal:
        return (self.pnl / self.count) if self.count else Decimal(0)

    @property
    def avg_win(self) -> Decimal:
        wins = self.wins
        return (sum((row.pnl for row in wins), Decimal(0)) / len(wins)) if wins else Decimal(0)

    @property
    def avg_loss(self) -> Decimal:
        losses = self.losses
        return (sum((row.pnl for row in losses), Decimal(0)) / len(losses)) if losses else Decimal(0)

    def exit_reasons(self, profitable: bool) -> list[tuple[str, int]]:
        source = self.wins if profitable else self.losses
        return Counter(row.exit_title for row in source).most_common()


def to_rows(positions: list[Position], native_decimals: int = 18) -> list[TradeRow]:
    return [
        TradeRow(
            position=position,
            spent=from_wei(position.native_spent_wei, native_decimals),
            returned=from_wei(position.native_returned_wei, native_decimals),
        )
        for position in positions
    ]


def summarize(positions: list[Position], label: str, native_decimals: int = 18) -> Summary:
    return Summary(label=label, rows=to_rows(positions, native_decimals))


def _line(row: TradeRow, symbol: str) -> str:
    icon = "🟢" if row.profitable else "🔴"
    when = (row.position.closed_at or row.position.opened_at).strftime("%d.%m %H:%M")
    return (f"{icon} {esc(row.position.token_symbol)} {row.pnl_pct:+.0f}% "
            f"({fmt_amount(row.pnl)} {symbol}) · {when} · {esc(row.exit_title)}")


def render_summary(summary: Summary, symbol: str, top: int = 5) -> str:
    """Блок отчёта по одному режиму."""
    if not summary.count:
        return f"<b>{esc(summary.label)}</b>: сделок нет"

    icon = "🟢" if summary.pnl >= 0 else "🔴"
    lines = [
        f"<b>{esc(summary.label)}</b>",
        f"Сделок: <b>{summary.count}</b> · прибыльных <b>{len(summary.wins)}</b> "
        f"({summary.winrate}%) · убыточных <b>{len(summary.losses)}</b>",
        f"{icon} Итог: <b>{fmt_amount(summary.pnl)} {symbol}</b> ({summary.pnl_pct:+.1f}%), "
        f"в среднем {fmt_amount(summary.average)} на сделку",
    ]
    if summary.wins and summary.losses:
        lines.append(f"Средняя прибыль {fmt_amount(summary.avg_win)} · "
                     f"средний убыток {fmt_amount(summary.avg_loss)}")

    if summary.wins:
        lines.append("\n🟢 <b>Плюсовые</b>")
        lines += [f"  {_line(row, symbol)}" for row in summary.wins[:top]]
        if len(summary.wins) > top:
            lines.append(f"  …и ещё {len(summary.wins) - top} — полный список в файле")

    if summary.losses:
        lines.append("\n🔴 <b>Убыточные</b>")
        lines += [f"  {_line(row, symbol)}" for row in summary.losses[:top]]
        if len(summary.losses) > top:
            lines.append(f"  …и ещё {len(summary.losses) - top} — полный список в файле")
        reasons = summary.exit_reasons(profitable=False)
        if reasons:
            lines.append("  Причины: " + ", ".join(f"{title} — {count}" for title, count in reasons))

    return "\n".join(lines)


def period_label(days: int | None, rows: list[TradeRow] | None = None,
                 now: dt.datetime | None = None) -> str:
    """«за всё время (с 12.08, 27 дн.)» либо «за 30 дн.»."""
    if days is not None:
        return f"за {days} дн."
    stamps = [row.position.closed_at or row.position.opened_at for row in (rows or [])]
    stamps = [stamp for stamp in stamps if stamp]
    if not stamps:
        return "за всё время"
    first = min(_aware(stamp) for stamp in stamps)
    span = ((now or dt.datetime.now(dt.UTC)) - first).days + 1
    return f"за всё время (с {first:%d.%m.%Y}, {span} дн.)"


def period_breakdown(rows: list[TradeRow], symbol: str,
                     now: dt.datetime | None = None) -> list[str]:
    """Как менялись результаты в последние окна — при накопленных данных полезнее всего."""
    now = now or dt.datetime.now(dt.UTC)
    lines = []
    for title, days in (("24 часа", 1), ("7 дней", 7), ("30 дней", 30)):
        since = now - dt.timedelta(days=days)
        window = [row for row in rows
                  if row.position.closed_at and _aware(row.position.closed_at) >= since]
        if not window:
            continue
        summary = Summary(label=title, rows=window)
        icon = "🟢" if summary.pnl >= 0 else "🔴"
        lines.append(f"{icon} {title}: {summary.count} сдел. · {summary.winrate}% плюсовых · "
                     f"{fmt_amount(summary.pnl)} {symbol}")
    return lines


def source_breakdown(rows: list[TradeRow], symbol: str) -> list[str]:
    """Что приносит деньги: снайп листингов, перехват разгона или ручные покупки.

    Это главный вопрос при выборе режима — общий итог его скрывает.
    """
    groups: dict[str, list[TradeRow]] = {}
    for row in rows:
        groups.setdefault(row.position.source or "manual", []).append(row)
    if len(groups) < 2:
        return []
    lines = []
    for source, group in sorted(groups.items(), key=lambda item: -len(item[1])):
        summary = Summary(label=source, rows=group)
        icon = "🟢" if summary.pnl >= 0 else "🔴"
        title = SOURCE_TITLES.get(source, source)
        lines.append(f"{icon} {title}: {summary.count} сдел. · {summary.winrate}% плюсовых · "
                     f"{fmt_amount(summary.pnl)} {symbol} "
                     f"({fmt_amount(summary.average)} на сделку)")
    return lines


def render_report(real: Summary, paper: Summary, days: int | None, symbol: str,
                  open_positions: int = 0, now: dt.datetime | None = None) -> str:
    """Полный отчёт: боевой режим и тестовый рядом."""
    label = period_label(days, [*real.rows, *paper.rows], now)
    parts = [f"🧾 <b>Отчёт по сделкам</b> {label}\n"]
    parts.append(render_summary(real, symbol))
    parts.append("\n" + render_summary(paper, symbol))

    if real.count and paper.count:
        better = "тестовые" if paper.average > real.average else "боевые"
        parts.append(
            f"\nНа сделку: боевые {fmt_amount(real.average)} {symbol} · "
            f"тестовые {fmt_amount(paper.average)} {symbol} → лучше идут <b>{better}</b>."
        )
    if real.count:
        windows = period_breakdown(real.rows, symbol, now)
        if windows:
            parts.append("\n📅 <b>Боевые по периодам</b>\n" + "\n".join(windows))

    by_source = source_breakdown([*real.rows, *paper.rows], symbol)
    if by_source:
        parts.append("\n🎯 <b>По способу входа</b>\n" + "\n".join(by_source))

    if open_positions:
        parts.append(f"\nОткрытых позиций сейчас: <b>{open_positions}</b> "
                     "(в расчёт не входят — /positions)")
    parts.append("\nПолный список сделок — в файле ниже. "
                 "Сузить период: <code>/report 7</code>")
    return "\n".join(parts)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def trades_csv(rows: list[TradeRow], open_rows: list[TradeRow] | None = None) -> str:
    """Выгрузка сделок для таблиц: одна строка на позицию."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([
        "статус", "режим", "открыта", "закрыта", "сеть", "токен", "адрес", "площадка",
        "источник", "A/B", "причина выхода", "вложено", "возвращено", "pnl", "pnl_%",
        "tx покупки", "tx продажи",
    ])
    for row in [*rows, *(open_rows or [])]:
        position = row.position
        venue = position.dex_kind + (f"/{position.pool_fee}" if position.pool_fee else "")
        writer.writerow([
            "закрыта" if position.status == "closed" else "открыта",
            "тест" if position.is_paper else "боевой",
            _stamp(position.opened_at),
            _stamp(position.closed_at),
            position.chain,
            position.token_symbol,
            position.token_address,
            venue,
            position.source,
            position.ab_group or "",
            row.exit_title if position.status == "closed" else "",
            _num(row.spent),
            _num(row.returned),
            _num(row.pnl),
            f"{row.pnl_pct:.2f}".replace(".", ","),
            position.buy_tx or "",
            position.sell_tx or "",
        ])
    return buffer.getvalue()


def _stamp(value: dt.datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def _num(value: Decimal) -> str:
    """Числа с запятой — так их понимает Excel с русской локалью."""
    return f"{value:.8f}".replace(".", ",")
