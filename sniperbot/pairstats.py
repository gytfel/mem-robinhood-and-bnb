"""Отчёт о качестве фильтров: что стало с токенами, которые бот пропустил и отсеял.

Обычная статистика отвечает на вопрос «сколько токенов отсеяно», но не на главный:
**правильно ли** они отсеяны. Ответ даёт только то, что случилось с ценой дальше —
поэтому бот наблюдает за ценой всех найденных пулов, а не только купленных.

Модуль ничего не знает ни про Telegram, ни про базу: на вход — строки о пулах,
на выход — готовый текст. Так расчёты можно проверить тестами.

Про честность выводов: разница долей почти всегда есть просто из-за случайности.
Поэтому каждое сравнение проходит проверку значимости, и там, где данных мало,
отчёт прямо говорит «разница в пределах погрешности», а не выдаёт шум за находку.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from sniperbot.sniper.safety import FILTER_TITLES
from sniperbot.utils.fmt import esc

MILESTONES: tuple[Decimal, ...] = (Decimal("1.5"), Decimal(2), Decimal(5))
TARGET = Decimal(2)              # по какой планке сравниваем фильтры между собой
MIN_SAMPLE = 30                  # меньше этого числа наблюдений вывод не делаем
Z_95 = 1.96                      # порог значимости для двусторонней проверки


@dataclass(slots=True)
class Outcome:
    """Судьба одного найденного пула."""

    accepted: bool                    # прошёл фильтры (куплен или мог быть куплен)
    multiple: Decimal                 # пик цены к первой замеченной, «×»
    codes: tuple[str, ...] = ()       # какие фильтры его отсеяли
    hour: int | None = None           # час суток UTC, когда пул найден


@dataclass(slots=True)
class Bucket:
    """Группа пулов и то, чем она закончилась."""

    label: str
    rows: list[Outcome] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)

    @property
    def median(self) -> Decimal:
        if not self.rows:
            return Decimal(0)
        value = statistics.median(float(row.multiple) for row in self.rows)
        return Decimal(str(round(value, 2)))

    def hits(self, milestone: Decimal = TARGET) -> int:
        return sum(1 for row in self.rows if row.multiple >= milestone)

    def share(self, milestone: Decimal = TARGET) -> Decimal:
        """Доля пулов, дошедших до планки, в процентах."""
        if not self.rows:
            return Decimal(0)
        return Decimal(self.hits(milestone) * 100) / Decimal(self.count)


def peak_multiple(pair) -> Decimal | None:  # noqa: ANN001 - строка SeenPair
    """Во сколько раз пик цены выше первой замеченной. None — данных нет."""
    first = getattr(pair, "first_price", None)
    peak = getattr(pair, "peak_price", None)
    if not first or not peak or first <= 0:
        return None
    return Decimal(peak) / Decimal(first)


def to_outcomes(pairs) -> list[Outcome]:
    """Отбирает пулы, по которым есть замеры цены, и приводит их к Outcome."""
    outcomes = []
    for pair in pairs:
        multiple = peak_multiple(pair)
        if multiple is None:
            continue
        codes = tuple(code for code in (getattr(pair, "reject_codes", "") or "").split(",") if code)
        created = getattr(pair, "created_at", None)
        outcomes.append(Outcome(
            accepted=pair.status == "sniped" or (pair.status != "rejected" and not codes),
            multiple=multiple,
            codes=codes,
            hour=created.hour if created is not None else None,
        ))
    return outcomes


def split(outcomes: list[Outcome]) -> tuple[Bucket, Bucket]:
    passed = Bucket("Прошли фильтр", [row for row in outcomes if row.accepted])
    denied = Bucket("Отклонены фильтром", [row for row in outcomes if not row.accepted])
    return passed, denied


# ------------------------------------------------------------------ значимость
def significant(a_hits: int, a_total: int, b_hits: int, b_total: int) -> bool:
    """Отличаются ли две доли настолько, что это вряд ли случайность.

    Обычная проверка двух пропорций по нормальному приближению. Нужна, чтобы не
    объявлять находкой разницу вроде «11% против 8%» на полусотне наблюдений.
    """
    if a_total < MIN_SAMPLE or b_total < MIN_SAMPLE:
        return False
    p1 = a_hits / a_total
    p2 = b_hits / b_total
    pooled = (a_hits + b_hits) / (a_total + b_total)
    if pooled in (0.0, 1.0):
        return False
    error = (pooled * (1 - pooled) * (1 / a_total + 1 / b_total)) ** 0.5
    if error == 0:
        return False
    return abs(p1 - p2) / error > Z_95


def compare_verdict(passed: Bucket, denied: Bucket, symbol: str = "×") -> str:
    """Вывод по главному сравнению: помогают фильтры или мешают."""
    if not passed.count or not denied.count:
        return "Сравнивать пока не с чем — нужны замеры и по пропущенным, и по отсеянным."

    good = passed.share()
    bad = denied.share()
    head = f"{good:.0f}% против {bad:.0f}%"
    if not significant(passed.hits(), passed.count, denied.hits(), denied.count):
        return f"{head} — разница в пределах погрешности, данных пока мало."
    if good > bad:
        return f"{head} — фильтры работают: пропущенное растёт чаще отсеянного."
    return (f"{head} — ⚠️ фильтры режут то, что растёт. "
            "Ослабьте самые красные строки ниже: /config filters")


# ------------------------------------------------------------- разрез по фильтрам
@dataclass(slots=True)
class FilterRow:
    code: str
    count: int
    share: Decimal
    mark: str          # 🟢 отсеивает слабое · 🔴 режет растущее · ⚪️ данных мало

    @property
    def title(self) -> str:
        return FILTER_TITLES.get(self.code, self.code)


def filter_rows(denied: Bucket, baseline: Bucket) -> list[FilterRow]:
    """Каждый фильтр отдельно: как рос тот, кого именно он отсёк.

    Сравнение идёт с пропущенными пулами: если отсеянные этим фильтром растут
    не хуже, фильтр отнимает прибыль, а не бережёт деньги.
    """
    groups: dict[str, list[Outcome]] = {}
    for row in denied.rows:
        for code in row.codes:
            groups.setdefault(code, []).append(row)

    base_hits, base_total = baseline.hits(), baseline.count
    rows = []
    for code, items in groups.items():
        bucket = Bucket(code, items)
        if not significant(bucket.hits(), bucket.count, base_hits, base_total):
            mark = "⚪️"
        elif bucket.share() < baseline.share():
            mark = "🟢"
        else:
            mark = "🔴"
        rows.append(FilterRow(code, bucket.count, bucket.share(), mark))
    return sorted(rows, key=lambda row: row.count, reverse=True)


# ---------------------------------------------------------------- разрез по часам
@dataclass(slots=True)
class HourRow:
    hour: int
    count: int
    share: Decimal


def hour_rows(outcomes: list[Outcome], minimum: int = MIN_SAMPLE) -> list[HourRow]:
    """Доля выросших токенов по часам суток (UTC)."""
    groups: dict[int, list[Outcome]] = {}
    for row in outcomes:
        if row.hour is not None:
            groups.setdefault(row.hour, []).append(row)
    rows = [HourRow(hour, len(items), Bucket(str(hour), items).share())
            for hour, items in groups.items() if len(items) >= minimum]
    return sorted(rows, key=lambda row: row.share, reverse=True)


def hours_verdict(rows: list[HourRow], outcomes: list[Outcome]) -> str:
    """Стоит ли вообще делить сутки на часы — или разница случайна."""
    if len(rows) < 4:
        return "Данных по часам мало — копятся."
    best, worst = rows[0], rows[-1]
    best_bucket = [row for row in outcomes if row.hour == best.hour]
    worst_bucket = [row for row in outcomes if row.hour == worst.hour]
    a, b = Bucket("best", best_bucket), Bucket("worst", worst_bucket)
    if not significant(a.hits(), a.count, b.hits(), b.count):
        return "Разница между часами в пределах погрешности."
    average = Bucket("all", outcomes).share()
    good = sorted(row.hour for row in rows if row.share >= average)
    return ("Разница между часами значима. Лучшие часы (UTC): "
            + ", ".join(f"{hour:02d}" for hour in good))


# ----------------------------------------------------------------------- отчёт
def render_bucket(bucket: Bucket) -> list[str]:
    if not bucket.count:
        return [f"<b>{esc(bucket.label)}</b>: замеров нет"]
    lines = [f"<b>{esc(bucket.label)}</b> ({bucket.count} шт)",
             f"   медиана пика  {bucket.median}×"]
    lines += [f"   дошли до {milestone}×  {bucket.share(milestone):.0f}%" for milestone in MILESTONES]
    return lines


def render_outcomes(outcomes: list[Outcome], seen: int, bought: int, window: str) -> str:
    """Полный разбор: что фильтры пропустили, что отсеяли и кто был прав."""
    passed, denied = split(outcomes)
    parts = [
        f"🔬 <b>Качество фильтров</b> {window}\n",
        f"Видел пулов: {seen} · с замерами цены: {len(outcomes)} · куплено: {bought}\n",
    ]
    parts += render_bucket(passed)
    parts.append("")
    parts += render_bucket(denied)
    parts.append(f"\n<b>Вывод</b>\n{compare_verdict(passed, denied)}")

    rows = filter_rows(denied, passed)
    if rows:
        parts.append(f"\n<b>Каждый фильтр отдельно</b>\n"
                     f"<i>доля {TARGET}× среди отсеянных им; "
                     f"у пропущенных {passed.share():.0f}%</i>\n")
        parts += [f"{row.mark} {row.share:5.0f}%  ({row.count} шт)  {esc(row.title)}"
                  for row in rows[:12]]
        parts.append("\n🟢 отсеивает слабое · 🔴 режет то, что растёт · ⚪️ данных мало")

    hours = hour_rows(outcomes)
    if hours:
        parts.append(f"\n<b>По часам суток</b> <i>(доля {TARGET}×, UTC)</i>")
        average = Bucket("all", outcomes).share()
        for row in [*hours[:4], *([HourRow(-1, 0, Decimal(0))] if len(hours) > 7 else []), *hours[-3:]]:
            if row.hour < 0:
                parts.append("   …")
                continue
            mark = "🟢" if row.share >= average else "🔴"
            parts.append(f"{mark} {row.hour:02d}:00  {row.share:.1f}%  ({row.count} шт)")
        parts.append(hours_verdict(hours, outcomes))

    return "\n".join(parts)


def round_trip_cost(gas_used: int, gas_price_wei: int, buy_amount: Decimal,
                    native_decimals: int = 18) -> tuple[Decimal, Decimal]:
    """Во что обходится круг «купил-продал»: (в монете, в процентах от входа).

    Издержки — то, что съедает прибыль ещё до движения цены: при малом входе
    даже хорошая сделка уходит в минус.
    """
    cost = Decimal(gas_used * gas_price_wei) / Decimal(10**native_decimals)
    if buy_amount <= 0:
        return cost, Decimal(0)
    return cost, cost / buy_amount * 100


def window_label(hours: int | None, rows: list | None = None,
                 now: dt.datetime | None = None) -> str:
    if hours:
        return f"за {hours} ч"
    stamps = [getattr(row, "created_at", None) for row in (rows or [])]
    stamps = [stamp for stamp in stamps if stamp]
    if not stamps:
        return "за всё время"
    first = min(stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.UTC) for stamp in stamps)
    days = ((now or dt.datetime.now(dt.UTC)) - first).days + 1
    return f"за всё время ({days} дн.)"
