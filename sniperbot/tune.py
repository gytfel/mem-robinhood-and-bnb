"""Подбор настроек по собственным сделкам.

Бот уже умеет считать, какие выходы дали бы лучший результат и в какие часы
сделки удачнее. Беда в том, что эти ответы лежат по разным командам и человек
доходит до них редко — а настройки тем временем остаются те, что были выбраны
в первый день.

Здесь всё это собирается в один список предложений, каждое из которых — готовое
значение для `/set`. Решение остаётся за человеком: бот ничего не меняет сам.

Модуль намеренно без базы и без Telegram — на вход числа, на выход предложения.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MIN_TRADES = 10          # меньше — подбор описывает случайность, а не поведение
TP_GRID = (50, 75, 100, 150, 200, 300, 500)
SL_GRID = (20, 30, 40, 50, 60, 70)
# Насколько предложение должно быть лучше текущего, чтобы его вообще показывать.
# Разница в полпроцента на сделку — это шум, а дёргать настройки из-за шума хуже,
# чем не трогать их вовсе.
MIN_GAIN_PCT = Decimal(3)


@dataclass(frozen=True, slots=True)
class Proposal:
    """Одно изменение настройки: что, на что и почему."""

    name: str        # имя для /set
    value: str       # новое значение в том виде, в каком его принимает /set
    reason: str      # объяснение человеку

    @property
    def command(self) -> str:
        return f"/set {self.name} {self.value}"


def simulate(peak: Decimal, final: Decimal, take_profit: int, stop_loss: int) -> Decimal:
    """Что дала бы сделка при заданных TP/SL: цель, стоп или фактический исход."""
    if peak >= take_profit:
        return Decimal(take_profit)
    if final <= -stop_loss:
        return Decimal(-stop_loss)
    return final


def best_exits(trades: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, int, int, int] | None:
    """Лучшая пара TP/SL по перебору: (средний итог, tp, sl, прибыльных сделок).

    ``trades`` — пары «максимальный рост, фактический итог» в процентах.
    """
    if not trades:
        return None
    best: tuple[Decimal, int, int, int] | None = None
    for take_profit in TP_GRID:
        for stop_loss in SL_GRID:
            results = [simulate(peak, final, take_profit, stop_loss) for peak, final in trades]
            average = sum(results, Decimal(0)) / len(results)
            wins = sum(1 for value in results if value > 0)
            if best is None or average > best[0]:
                best = (average, take_profit, stop_loss, wins)
    return best


def current_average(trades: list[tuple[Decimal, Decimal]]) -> Decimal:
    if not trades:
        return Decimal(0)
    return sum((final for _peak, final in trades), Decimal(0)) / len(trades)


def exit_proposals(trades: list[tuple[Decimal, Decimal]], cfg) -> list[Proposal]:  # noqa: ANN001
    """Предложения по тейку и стопу, если перебор нашёл заметно лучшее."""
    if len(trades) < MIN_TRADES:
        return []
    best = best_exits(trades)
    if best is None:
        return []
    average, take_profit, stop_loss, wins = best
    if average - current_average(trades) < MIN_GAIN_PCT:
        return []

    reason = (f"на ваших {len(trades)} сделках это дало бы {average:+.1f}% вместо "
              f"{current_average(trades):+.1f}% на сделку (прибыльных {wins} из {len(trades)})")
    found: list[Proposal] = []
    # Лесенка тейка и одиночный TP — одна и та же настройка, и переписывать
    # рабочую лесенку одним числом нельзя: это заметное изменение стратегии.
    ladder = (getattr(cfg, "tp_ladder", "") or "").strip()
    if not ladder and int(getattr(cfg, "take_profit_pct", 0) or 0) != take_profit:
        found.append(Proposal("tp", str(take_profit), reason))
    if int(getattr(cfg, "stop_loss_pct", 0) or 0) != stop_loss:
        found.append(Proposal("sl", str(stop_loss), reason if not found else "та же прикидка"))
    return found


def secure_proposal(trigger: int, delta: Decimal, current: int, symbol: str) -> Proposal | None:
    """Порог возврата вложенного, если он что-то даёт и отличается от текущего."""
    if trigger <= 0 or delta <= 0 or trigger == current:
        return None
    return Proposal("secure", str(trigger),
                    f"возврат вложенного на +{trigger}% дал бы "
                    f"{delta:+.4f} {symbol} на прошлых сделках")


def hours_proposal(spec: str, current: str) -> Proposal | None:
    """Часы торговли, если данные их выделяют, а сейчас ограничения нет."""
    spec = (spec or "").strip()
    if not spec or spec == (current or "").strip():
        return None
    return Proposal("hours", spec, f"в эти часы ваши сделки заметно удачнее ({spec})")


def render(proposals: list[Proposal], label: str) -> str:
    """Текст письма с предложениями. Пустой список — письма не будет."""
    if not proposals:
        return ""
    lines = [f"🔧 <b>Подстройка по вашим сделкам</b> {label}\n",
             "Данные за период говорят вот что:"]
    for item in proposals:
        lines.append(f"\n· <code>{item.command}</code>\n  {item.reason}")
    lines.append("\nПрименить всё разом — кнопка ниже. Или по одной командой выше.")
    lines.append("<i>Это прикидка на прошлых сделках: без учёта проскальзывания и "
                 "того, что рынок меняется. Не гарантия, а повод посмотреть.</i>")
    return "\n".join(lines)
