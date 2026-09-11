"""Монитор позиций: лестница фиксаций, безубыток, защита от слива и стопы.

Правила выхода вынесены в чистую функцию :func:`decide_exit` — её легко
проверить тестами на всех сценариях, не поднимая ни ноды, ни базы.

Порядок важен: сначала спасаем деньги (слив ликвидности, стоп, безубыток),
и только потом фиксируем прибыль.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.dex_adapter import PoolRef
from sniperbot.config import Settings
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position, User
from sniperbot.notify import Notifier
from sniperbot.settings_registry import ladder_steps
from sniperbot.sniper.executor import Trader
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

MAX_QUOTE_FAILURES = 20
SELL_GAS_UNITS = 250_000     # типичный расход газа на продажу с запасом
GAS_CACHE_SECONDS = 60.0     # цена газа меняется медленнее, чем опрашиваются позиции


def check_interval(age_minutes: float, fast_interval: float, normal_interval: float,
                   fast_window_minutes: float) -> float:
    """Как часто проверять позицию.

    Основные потери случаются в первые минуты жизни токена: цена успевает
    сложиться в разы между двумя редкими проверками, и стоп-лосс исполняется
    уже на дне. Поэтому свежие позиции опрашиваются часто, а старые — редко,
    чтобы не жечь лимиты RPC.
    """
    if fast_window_minutes > 0 and age_minutes <= fast_window_minutes:
        return fast_interval
    return normal_interval


@dataclass(slots=True)
class Rule:
    key: str
    title: str
    icon: str


@dataclass(slots=True)
class ExitContext:
    """Всё, что нужно знать о позиции, чтобы решить — выходить или держать."""

    change: Decimal                      # текущий результат в процентах от входа
    peak_change: Decimal                 # лучший результат за жизнь позиции
    price: Decimal
    peak_price: Decimal
    liquidity: Decimal | None = None
    peak_liquidity: Decimal | None = None
    age_minutes: float = 0.0
    exit_cost: Decimal = Decimal(0)      # во что обойдётся сама продажа (газ)


RULE_RUG = Rule("rug", "Ликвидность уходит из пула", "🚨")
RULE_STOP = Rule("stop_loss", "Стоп-лосс", "🛑")
RULE_BREAKEVEN = Rule("breakeven", "Выход в безубыток", "🟡")
RULE_LADDER = Rule("ladder", "Ступень фиксации", "🪜")
RULE_TAKE = Rule("take_profit", "Тейк-профит", "🎉")
RULE_TRAIL = Rule("trailing", "Трейлинг-стоп", "📉")
RULE_DEAD = Rule("dead", "Позиция не растёт", "🥱")
RULE_COLLAPSE = Rule("collapse", "Обвал цены — стоп не успел", "💥")
RULE_SECURE = Rule("secure", "Возврат вложенного", "🛟")

# После возврата вложенного должно остаться хоть что-то: если продать
# приходится почти всё, это уже не частичная фиксация, а обычный выход.
MIN_REMAINDER_PCT = 5

# Падение глубже этого порога за один шаг наблюдения — это не движение цены,
# а вынутая ликвидность: между проверками промежуточных значений не было.
COLLAPSE_PCT = Decimal(-85)


def ladder_percent(position: Position, share: int) -> int:
    """Доля ступени считается от исходного объёма, а продаём от остатка."""
    bought = position.bought_wei or position.amount_wei
    current = position.amount_wei
    if current <= 0 or bought <= 0:
        return 100
    wanted = bought * share // 100
    if wanted >= current:
        return 100
    return max(1, min(100, math.ceil(wanted * 100 / current)))


def secure_share(need: Decimal, value: Decimal) -> int:
    """Какую долю остатка продать, чтобы вернуть вложенное. 0 — не сейчас.

    Ступень лестницы считает долю от размера позиции, и этого мало: продать 40%
    при росте +50% значит вернуть 60% вложенного, а не всё. Здесь доля считается
    от денег: сколько нужно вернуть, делённое на то, сколько стоит остаток.
    После такой продажи сделка уже не может закончиться в минусе — что бы ни
    случилось с остатком.
    """
    if need <= 0 or value <= 0:
        return 0
    share = need / value * 100
    if share > 100 - MIN_REMAINDER_PCT:
        return 0          # рост ещё не покрывает вложенное вместе с расходами
    return max(1, int(math.ceil(share)))


def retired_steps(position: Position, sold_percent: int) -> list[str]:
    """Ступени, которые возврат вложенного уже продал за них.

    Лестница и возврат черпают из одной позиции, а доля ступени считается от
    исходного объёма. Если не погасить перекрытые ступени, следующая проверка
    продаст ими тот самый хвост, ради которого всё и делалось: ступень «40% от
    исходного» после возврата 72% требует больше, чем осталось.
    """
    bought = position.bought_wei or position.amount_wei
    if bought <= 0:
        return ["secure"]
    sold = Decimal(sold_percent) * position.amount_wei / bought   # доля от исходного объёма

    covered: list[str] = []
    total = Decimal(0)
    for growth, share in ladder_steps(position.tp_ladder):
        total += share
        if total > sold:
            break
        covered.append(str(growth))
    return ["secure", *covered]


def decide_exit(position: Position, ctx: ExitContext) -> tuple[Rule | None, int, str]:
    """Решение о выходе: (правило, процент продажи, метка сработавшей ступени)."""
    if not position.auto_sell or position.amount_wei <= 0:
        return None, 0, ""

    # 1. Из пула вынимают ликвидность — выходим не раздумывая.
    rug = int(position.rug_guard_pct or 0)
    if rug and ctx.liquidity is not None and ctx.peak_liquidity and ctx.peak_liquidity > 0:
        drop = (ctx.peak_liquidity - ctx.liquidity) / ctx.peak_liquidity * 100
        if drop >= rug:
            return RULE_RUG, 100, ""

    # 2. Стоп-лосс. Если цена рухнула далеко за его уровень, называем вещи своими
    # именами: сработать раньше стоп не мог, между проверками не было цены между.
    if position.stop_loss_pct and ctx.change <= -Decimal(position.stop_loss_pct):
        deep = ctx.change <= COLLAPSE_PCT and -Decimal(position.stop_loss_pct) > COLLAPSE_PCT
        return (RULE_COLLAPSE if deep else RULE_STOP), 100, ""

    # 3. Безубыток: цель уже была достигнута, теперь не даём уйти в минус.
    if position.breakeven_armed and ctx.change <= 0:
        return RULE_BREAKEVEN, 100, ""

    done = {step.strip() for step in (position.tp_done or "").split(",") if step.strip()}

    # 4. Возврат вложенного: продаём ровно столько, чтобы вернуть свои деньги.
    # Идёт раньше лестницы — сначала сделка перестаёт быть убыточной, и только
    # потом имеет смысл фиксировать прибыль.
    secure = int(position.secure_pct or 0)
    if secure and "secure" not in done and ctx.change >= secure:
        need = (from_wei(position.native_spent_wei or 0)
                - from_wei(position.native_returned_wei or 0) + ctx.exit_cost)
        value = from_wei(position.amount_wei, position.token_decimals or 18) * ctx.price
        share = secure_share(need, value)
        if share:
            return RULE_SECURE, share, "secure"

    # 5. Лестница фиксаций — по одной ступени за проверку.
    for growth, share in ladder_steps(position.tp_ladder):
        marker = str(growth)
        if marker in done:
            continue
        if ctx.change >= growth:
            return RULE_LADDER, ladder_percent(position, share), marker

    # 6. Обычный тейк-профит (если лестница не задана).
    if not position.tp_ladder and position.take_profit_pct and ctx.change >= position.take_profit_pct:
        return RULE_TAKE, max(1, min(100, position.sell_percent or 100)), ""

    # 7. Трейлинг-стоп от максимума.
    if position.trailing_stop_pct and ctx.peak_price > 0:
        drop = (ctx.peak_price - ctx.price) / ctx.peak_price * 100
        if drop >= Decimal(position.trailing_stop_pct) and ctx.change > 0:
            return RULE_TRAIL, 100, ""

    # 8. Позиция висит и не растёт — освобождаем деньги.
    timeout = int(position.dead_timeout_min or 0)
    if timeout and ctx.age_minutes >= timeout and ctx.peak_change < Decimal(position.dead_min_pct or 0):
        return RULE_DEAD, 100, ""

    return None, 0, ""


class PositionMonitor:
    """Периодически переоценивает позиции и закрывает их по правилам выхода."""

    def __init__(self, registry: ChainRegistry, trader: Trader, notifier: Notifier,
                 settings: Settings) -> None:
        self.registry = registry
        self.trader = trader
        self.notifier = notifier
        self.settings = settings
        self._running = False
        self._failures: dict[int, int] = {}
        self._last_check: dict[int, float] = {}
        self._gas: dict[str, tuple[float, Decimal]] = {}

    async def run(self) -> None:
        self._running = True
        log.info(
            "Монитор позиций запущен: свежие каждые %.1f c (%.0f мин), остальные каждые %.1f c",
            self.settings.fast_poll_interval, self.settings.fast_watch_minutes,
            self.settings.position_poll_interval,
        )
        step = min(self.settings.fast_poll_interval, self.settings.position_poll_interval)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Монитор позиций: %s", exc)
            await asyncio.sleep(step)

    def stop(self) -> None:
        self._running = False

    async def tick(self) -> None:
        async with session_scope() as session:
            positions = await repo.open_positions(session)

        now = asyncio.get_running_loop().time()
        alive = {position.id for position in positions}
        self._last_check = {key: value for key, value in self._last_check.items() if key in alive}

        due = []
        for position in positions:
            age = (dt.datetime.now(dt.UTC) - _aware(position.opened_at)).total_seconds() / 60
            interval = check_interval(
                age, self.settings.fast_poll_interval, self.settings.position_poll_interval,
                self.settings.fast_watch_minutes,
            )
            if now - self._last_check.get(position.id, 0.0) >= interval:
                self._last_check[position.id] = now
                due.append(position)

        # Свежие позиции проверяем разом: последовательный обход стоит секунд,
        # а на молодом токене каждая секунда — это проценты цены.
        results = await asyncio.gather(*(self.check_position(p) for p in due),
                                       return_exceptions=True)
        for position, result in zip(due, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                log.debug("Позиция #%s: %s", position.id, result)

    # --------------------------------------------------------------- оценка
    def _pool_of(self, position: Position) -> PoolRef:
        return PoolRef(address=position.pair_address or "", kind=position.dex_kind or "v2",
                       fee=position.pool_fee or 0)

    async def current_price(self, position: Position) -> Decimal | None:
        """Цена выхода: сколько нативной монеты дадут за весь остаток позиции."""
        if position.amount_wei <= 0:
            return None
        client = self.registry.get(position.chain)
        adapter = self.trader.adapter_for_position(position)
        native_out = await adapter.quote_sell(position.token_address, position.amount_wei,
                                              self._pool_of(position))
        tokens = from_wei(position.amount_wei, position.token_decimals)
        if tokens <= 0:
            return None
        return from_wei(native_out, client.config.native_decimals) / tokens

    async def _liquidity(self, position: Position) -> Decimal | None:
        if not position.rug_guard_pct or not position.pair_address:
            return None
        try:
            adapter = self.trader.adapter_for_position(position)
            state = await adapter.pool_state(position.token_address, self._pool_of(position),
                                             position.token_decimals)
        except Exception as exc:  # noqa: BLE001 - пул мог исчезнуть
            # Не ноль, а «не знаю»: сбой сети — не повод продавать живую позицию.
            # Настоящий слив покажет успешный ответ с пустыми резервами.
            log.debug("Ликвидность позиции #%s недоступна: %s", position.id, exc)
            return None
        return state.liquidity_native

    async def exit_cost(self, chain_key: str) -> Decimal:
        """Во что обойдётся продажа. Возврат вложенного обязан учесть и это.

        Газ — постоянная величина на сделку, и при малом входе он заметная доля
        вложенного: вернуть «ровно потраченное» без него значит остаться в минусе
        на стоимость самой транзакции.
        """
        now = asyncio.get_running_loop().time()
        cached = self._gas.get(chain_key)
        if cached is not None and now - cached[0] < GAS_CACHE_SECONDS:
            return cached[1]
        try:
            client = self.registry.get(chain_key)
            fees = await client.gas_fees()
            price = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0)
            cost = from_wei(price * SELL_GAS_UNITS, client.config.native_decimals)
        except Exception as exc:  # noqa: BLE001 - без цены газа правило просто строже
            log.debug("Цена газа для %s недоступна: %s", chain_key, exc)
            cost = self._gas.get(chain_key, (0.0, Decimal(0)))[1]
        self._gas[chain_key] = (now, cost)
        return cost

    async def check_position(self, position: Position) -> None:
        if position.amount_wei <= 0 or not position.entry_price:
            return
        try:
            price = await self.current_price(position)
        except Exception as exc:  # noqa: BLE001 - пул мог опустеть
            failures = self._failures.get(position.id, 0) + 1
            self._failures[position.id] = failures
            if failures == MAX_QUOTE_FAILURES:
                await self.notifier.send(
                    position.user_id,
                    f"⚠️ Не могу оценить позицию #{position.id} ({esc(position.token_symbol)}): {esc(exc)}\n"
                    "Похоже, ликвидность вытащили (rug). Проверьте вручную.",
                )
            return
        if price is None:
            return
        self._failures.pop(position.id, None)

        entry = position.entry_price
        change = (price / entry - 1) * 100 if entry > 0 else Decimal(0)
        peak_price = max(position.peak_price or price, price)
        peak_change = (peak_price / entry - 1) * 100 if entry > 0 else Decimal(0)
        liquidity = await self._liquidity(position)
        peak_liquidity = max(from_wei(position.peak_liquidity_wei or 0), liquidity or Decimal(0))
        age = (dt.datetime.now(dt.UTC) - _aware(position.opened_at)).total_seconds() / 60

        armed = position.breakeven_armed or bool(
            position.breakeven_pct and change >= Decimal(position.breakeven_pct)
        )
        newly_armed = armed and not position.breakeven_armed

        async with session_scope() as session:
            stored = await session.get(Position, position.id)
            if stored is None or stored.status != "open":
                return
            stored.last_price = price
            stored.peak_price = peak_price
            stored.breakeven_armed = armed
            if liquidity is not None:
                from sniperbot.utils.fmt import to_wei

                stored.peak_liquidity_wei = to_wei(peak_liquidity)
        position.breakeven_armed = armed

        if newly_armed:
            await self.notifier.send(
                position.user_id,
                f"🟡 {esc(position.token_symbol)}: +{change:.0f}% — стоп переведён в безубыток. "
                "Дальше эта сделка уже не может стать убыточной.",
            )

        cost = await self.exit_cost(position.chain) if position.secure_pct else Decimal(0)
        rule, percent, marker = decide_exit(
            position,
            ExitContext(change=change, peak_change=peak_change, price=price, peak_price=peak_price,
                        liquidity=liquidity, peak_liquidity=peak_liquidity, age_minutes=age,
                        exit_cost=cost),
        )
        if rule is None:
            return
        # Метки считаем до продажи: объём позиции нужен тот, что был на входе в правило.
        markers = retired_steps(position, percent) if rule is RULE_SECURE else (
            [marker] if marker else [])

        async with session_scope() as session:
            user = await session.get(User, position.user_id)
            cfg = await repo.get_settings(session, position.user_id, position.chain)
            fresh = await session.get(Position, position.id)
        if user is None or fresh is None or fresh.status != "open":
            return

        await self.notifier.send(
            position.user_id,
            f"{rule.icon} <b>{rule.title}</b> по {esc(position.token_symbol)} ({change:+.1f}%)\n"
            + (f"Продаю {percent}% — столько, чтобы вернуть вложенное. "
               f"Остальные {100 - percent}% остаются в позиции."
               if rule is RULE_SECURE else f"Продаю {percent}% позиции…"),
        )
        result = await self.trader.sell(user, fresh, cfg=cfg, percent=percent, reason=rule.key)
        symbol = self.registry.config(position.chain).native_symbol

        if not result.ok:
            await self.notifier.send(
                position.user_id,
                f"❌ Не удалось продать {esc(position.token_symbol)}: {esc(result.error)}",
            )
            return

        await self.notifier.send(
            position.user_id,
            f"✅ Продано {percent}% {esc(position.token_symbol)}\n"
            f"Получено: {fmt_amount(from_wei(result.amount_out))} {symbol}"
            + (f"\n\n🛟 Вложенное вернулось — что бы дальше ни случилось, "
               f"эта сделка уже не убыточна. Остаток {100 - percent}% "
               "едет дальше со стопом в безубытке."
               if rule is RULE_SECURE else "")
            + (f"\n<a href='{result.explorer_url}'>Транзакция</a>" if result.explorer_url else ""),
        )

        if markers:
            await self._mark_ladder_step(position.id, *markers)

    async def _mark_ladder_step(self, position_id: int, *markers: str) -> None:
        """Помечает ступени как сработавшие, чтобы они не повторились."""
        async with session_scope() as session:
            stored = await session.get(Position, position_id)
            if stored is None:
                return
            done = [step for step in (stored.tp_done or "").split(",") if step]
            for marker in markers:
                if marker not in done:
                    done.append(marker)
            stored.tp_done = ",".join(done)
            # После первой фиксации прибыль уже снята — защищаем остаток.
            if not stored.breakeven_armed:
                stored.breakeven_armed = True


def _aware(value):  # noqa: ANN001 - SQLite отдаёт наивные даты
    return value if value and value.tzinfo else (value or dt.datetime.now(dt.UTC)).replace(tzinfo=dt.UTC)
