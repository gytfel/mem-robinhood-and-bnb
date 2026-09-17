"""Раз в неделю: что говорят ваши собственные сделки.

Бот умеет считать, какие выходы дали бы лучший результат и в какие часы сделки
удачнее, но эти ответы лежат в командах, до которых редко доходят руки. А
настройки тем временем остаются те, что выбраны в первый день.

Поэтому раз в неделю бот сам приносит короткий список: что изменить и почему.
Менять он ничего не станет — кнопка «Применить» есть, и нажимает её человек.
Если данных мало или предложить нечего, письма просто не будет: регулярное
сообщение «всё хорошо» перестают читать через месяц.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging

from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.pairstats import good_hours, hour_rows, hours_spec, hours_verdict, to_outcomes
from sniperbot.reports import secure_grid, to_rows
from sniperbot.tune import Proposal, exit_proposals, hours_proposal, render, secure_proposal
from sniperbot.utils.fmt import from_wei

log = logging.getLogger(__name__)

CHECK_INTERVAL = 3600.0                  # раз в час смотрим, кому пора
PERIOD = dt.timedelta(days=7)
WINDOW = dt.timedelta(days=30)           # на какой глубине сделок считаем
SENT_KEY = "tune_sent"                   # когда в последний раз писали
PENDING_KEY = "tune_pending"             # что предложили и ещё не применили


def sent_key(user_id: int) -> str:
    return f"{SENT_KEY}:{user_id}"


def pending_key(user_id: int) -> str:
    return f"{PENDING_KEY}:{user_id}"


def pack(proposals: list[Proposal]) -> str:
    return json.dumps([[item.name, item.value] for item in proposals], ensure_ascii=False)


def unpack(raw: str | None) -> list[tuple[str, str]]:
    """Разбирает сохранённые предложения. Мусор в базе — это просто «нечего»."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    found = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, list) and len(item) == 2 and all(isinstance(x, str) for x in item):
            found.append((item[0], item[1]))
    return found


class WeeklyTuner:
    """Считает предложения по настройкам и приносит их раз в неделю."""

    def __init__(self, registry, notifier, settings) -> None:  # noqa: ANN001
        self.registry = registry
        self.notifier = notifier
        self.settings = settings
        self._running = False

    async def run(self) -> None:
        self._running = True
        log.info("Недельная подстройка запущена: проверка раз в час")
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - задача не должна умирать
                log.exception("Недельная подстройка: %s", exc)
            await asyncio.sleep(CHECK_INTERVAL)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> int:
        """Кому пора — тем и пишем. Возвращает число отправленных писем."""
        now = dt.datetime.now(dt.UTC)
        async with session_scope() as session:
            users = await repo.list_users(session, limit=500)
            due = []
            for user in users:
                if user.is_blocked:
                    continue
                if not self._due(await repo.get_state(session, sent_key(user.id)), now):
                    continue
                due.append((user.id, user.active_chain))

        sent = 0
        for user_id, chain_key in due:
            if await self.send_for(user_id, chain_key, now):
                sent += 1
        return sent

    def _due(self, stamp: str | None, now: dt.datetime) -> bool:
        if not stamp:
            return True
        try:
            last = dt.datetime.fromisoformat(stamp)
        except ValueError:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.UTC)
        return now - last >= PERIOD

    async def send_for(self, user_id: int, chain_key: str, now: dt.datetime) -> bool:
        """Собирает предложения и шлёт письмо. False — писать было нечего."""
        proposals = await self.proposals_for(user_id, chain_key)
        # Отметку ставим в любом случае: пересчитывать то же самое каждый час,
        # пока сделок не прибавилось, — пустая работа.
        async with session_scope() as session:
            await repo.set_state(session, sent_key(user_id), now.isoformat())
            await repo.set_state(session, pending_key(user_id), pack(proposals))
        if not proposals:
            return False

        from sniperbot.bot.keyboards import tune_kb

        await self.notifier.send(user_id, render(proposals, f"за {WINDOW.days} дн."),
                                 reply_markup=tune_kb())
        log.info("Подстройка для %s: предложений %s", user_id, len(proposals))
        return True

    async def proposals_for(self, user_id: int, chain_key: str) -> list[Proposal]:
        """Что стоит поменять — по закрытым сделкам и наблюдениям за пулами."""
        since = dt.datetime.now(dt.UTC) - WINDOW
        async with session_scope() as session:
            cfg = await repo.get_settings(session, user_id, chain_key)
            positions = await repo.closed_between(session, user_id, since, paper=False)
            outcomes = to_outcomes(await repo.outcome_pairs(session, chain_key, since))
            symbol = self.registry.config(chain_key).native_symbol
            found = self._from_trades(positions, cfg, symbol)
            hours = hours_proposal(self._hours_spec(outcomes), getattr(cfg, "trade_hours", ""))
        if hours is not None:
            found.append(hours)
        return found

    def _from_trades(self, positions: list, cfg, symbol: str) -> list[Proposal]:  # noqa: ANN001
        usable = [p for p in positions if p.entry_price and p.entry_price > 0 and p.peak_price]
        if not usable:
            return []
        trades = []
        for position in usable:
            peak = (position.peak_price / position.entry_price - 1) * 100
            spent = from_wei(position.native_spent_wei)
            final = ((from_wei(position.native_returned_wei) / spent - 1) * 100) if spent else 0
            trades.append((peak, final))

        found = exit_proposals(trades, cfg)
        rows = to_rows(usable)
        grid = [item for item in secure_grid(rows) if item.touched]
        if grid:
            best = max(grid, key=lambda item: item.delta)
            secure = secure_proposal(best.trigger, best.delta,
                                     int(getattr(cfg, "secure_pct", 0) or 0), symbol)
            if secure is not None:
                found.append(secure)
        return found

    def _hours_spec(self, outcomes: list) -> str:
        """Часы предлагаем только когда разница прошла проверку значимости."""
        rows = hour_rows(outcomes)
        if not rows or not hours_verdict(rows, outcomes).endswith("значима."):
            return ""
        return hours_spec(good_hours(rows, outcomes))
