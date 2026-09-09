"""Перехват токенов на разгоне: покупка не на листинге, а в начале движения.

Снайп новых пар ловит момент листинга — там почти всё решает случай. Этот режим
работает по уже торгующимся пулам: он смотрит, что происходит в них прямо сейчас,
и ищет момент, когда покупки перевешивают продажи, цена уже подросла, но ещё не
улетела.

Всё считается из событий Swap: одним запросом `eth_getLogs` со списком адресов
пулов получаем и объём, и направление сделок, и цену. Резервы читаются только для
финалистов, поэтому режим почти не нагружает RPC.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal

from sniperbot.chain.abi import V2_SWAP_TOPIC, V3_SWAP_TOPIC

log = logging.getLogger(__name__)

# В Uniswap V2/V3 token0 — меньший по адресу, поэтому порядок известен без запроса.
UINT256_HALF = 2**255


@dataclass(slots=True)
class PoolStats:
    """Что произошло в пуле за окно наблюдения."""

    pool: str
    swaps: int = 0
    buys: int = 0
    sells: int = 0
    volume_native: int = 0        # оборот в нативной монете, wei
    last_price: Decimal | None = None   # нативная монета за единицу токена (сырые единицы)

    @property
    def buy_ratio(self) -> Decimal:
        return Decimal(self.buys) / Decimal(self.swaps) if self.swaps else Decimal(0)


@dataclass(slots=True)
class MomentumSignal:
    """Оценка разгона по двум замерам."""

    pool: str
    gain_pct: Decimal = Decimal(0)
    buy_ratio: Decimal = Decimal(0)
    trades: int = 0
    volume_native: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    @property
    def score(self) -> int:
        """Грубый рейтинг для сортировки кандидатов."""
        gain = min(Decimal(100), max(Decimal(0), self.gain_pct))
        ratio = self.buy_ratio * 100
        activity = min(Decimal(100), Decimal(self.trades) * 5)
        return int(gain * Decimal("0.4") + ratio * Decimal("0.35") + activity * Decimal("0.25"))


def token_is_token0(token: str, quote: str) -> bool:
    """В паре token0 — меньший по адресу; это правило пула, а не запрос к ноде."""
    return token.lower() < quote.lower()


def _to_int(value) -> int:  # noqa: ANN001 - из лога приходят bytes или hex-строка
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int.from_bytes(bytes(value), "big")


def _signed(value: int) -> int:
    """int256 из лога V3: старший бит — знак."""
    return value - 2**256 if value >= UINT256_HALF else value


def parse_swap(log_entry: dict, token_is_first: bool, kind: str = "v2") -> tuple[str, int, int] | None:
    """Разбирает Swap: (направление, объём нативной монеты, объём токена).

    Направление — «buy», если нативная монета зашла в пул, то есть кто-то купил токен.
    """
    data = log_entry.get("data")
    raw = bytes(data) if not isinstance(data, str) else bytes.fromhex(data.removeprefix("0x"))
    words = [raw[index : index + 32] for index in range(0, len(raw), 32)]

    if kind == "v3":
        if len(words) < 2:
            return None
        amount0, amount1 = _signed(_to_int(words[0])), _signed(_to_int(words[1]))
        token_amount, native_amount = (amount0, amount1) if token_is_first else (amount1, amount0)
        if native_amount == 0 or token_amount == 0:
            return None
        # Положительное значение — актив зашёл в пул.
        side = "buy" if native_amount > 0 else "sell"
        return side, abs(native_amount), abs(token_amount)

    if len(words) < 4:
        return None
    amount0_in, amount1_in, amount0_out, amount1_out = (_to_int(word) for word in words[:4])
    if token_is_first:
        token_in, native_in, token_out, native_out = amount0_in, amount1_in, amount0_out, amount1_out
    else:
        native_in, token_in, native_out, token_out = amount0_in, amount1_in, amount0_out, amount1_out

    if native_in and token_out:
        return "buy", native_in, token_out
    if token_in and native_out:
        return "sell", native_out, token_in
    return None


def aggregate_swaps(logs: list[dict], pools: dict[str, dict]) -> dict[str, PoolStats]:
    """Сводит логи Swap по пулам: сколько сделок, куда и по какой цене.

    ``pools`` — карта «адрес пула → {token, quote, kind}».
    """
    stats: dict[str, PoolStats] = {}
    for entry in logs:
        address = str(entry.get("address", "")).lower()
        meta = pools.get(address)
        if meta is None:
            continue
        parsed = parse_swap(entry, token_is_token0(meta["token"], meta["quote"]), meta.get("kind", "v2"))
        if parsed is None:
            continue
        side, native_amount, token_amount = parsed

        bucket = stats.setdefault(address, PoolStats(pool=address))
        bucket.swaps += 1
        bucket.volume_native += native_amount
        if side == "buy":
            bucket.buys += 1
        else:
            bucket.sells += 1
        if token_amount:
            bucket.last_price = Decimal(native_amount) / Decimal(token_amount)
    return stats


def evaluate_momentum(
    previous_price: Decimal | None,
    stats: PoolStats,
    *,
    min_gain_pct: int,
    max_gain_pct: int,
    min_trades: int,
    min_buy_ratio_pct: int,
    min_volume_wei: int = 0,
) -> MomentumSignal:
    """Решает, похоже ли происходящее в пуле на начало движения.

    Условия намеренно двусторонние: рост должен быть заметным, но не уже
    случившимся — покупать на вершине хуже, чем не покупать вовсе.
    """
    signal = MomentumSignal(
        pool=stats.pool, trades=stats.swaps, volume_native=stats.volume_native,
        buy_ratio=stats.buy_ratio,
    )

    if previous_price and previous_price > 0 and stats.last_price:
        signal.gain_pct = (stats.last_price / previous_price - 1) * 100
    elif not previous_price:
        signal.reasons.append("нет предыдущего замера цены")

    if stats.swaps < min_trades:
        signal.reasons.append(f"мало сделок: {stats.swaps} < {min_trades}")
    if signal.buy_ratio * 100 < min_buy_ratio_pct:
        signal.reasons.append(
            f"покупок {signal.buy_ratio * 100:.0f}% < {min_buy_ratio_pct}% — продают больше, чем берут"
        )
    if signal.gain_pct < min_gain_pct:
        signal.reasons.append(f"рост {signal.gain_pct:.1f}% < {min_gain_pct}% — движения пока нет")
    if max_gain_pct and signal.gain_pct > max_gain_pct:
        signal.reasons.append(f"рост {signal.gain_pct:.0f}% > {max_gain_pct}% — заход уже на вершине")
    if min_volume_wei and stats.volume_native < min_volume_wei:
        signal.reasons.append("оборот за окно ниже порога")
    return signal


def sample_age(sample_time: dt.datetime, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.UTC)
    stamp = sample_time if sample_time.tzinfo else sample_time.replace(tzinfo=dt.UTC)
    return (now - stamp).total_seconds()


SWAP_TOPICS = {"v2": V2_SWAP_TOPIC, "v3": V3_SWAP_TOPIC}
