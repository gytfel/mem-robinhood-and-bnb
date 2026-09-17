"""Кошельки, за которыми стоит идти следом.

Снайп новой пары — ставка на то, что токен окажется живым. Покупка следом за
кошельком, который уже много раз угадывал, — ставка на чужой отбор, и она
дешевле: работу по выбору токена делает кто-то другой, а бот только повторяет.

Чтобы это не превратилось в «повторяю за случайным адресом», кошелёк получает
право на доверие только по результату: его покупки оцениваются тем же
двухпропорционным z-тестом, что и остальная статистика бота. Пока сделок мало,
кошелёк не проходит — совпадение из трёх удачных входов выглядит точно так же,
как мастерство.

Модуль не знает ни про базу, ни про сеть: на вход — разобранные логи и записи
сделок, на выход — адреса и оценки.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sniperbot.sniper.momentum import parse_swap, token_is_token0
from sniperbot.utils.evm import to_checksum

# Покупка мельче этой — пыль или тест контракта, а не решение человека.
MIN_BUY_NATIVE = Decimal("0.01")
# Раньше этого срока судить о покупке рано: движение мемкоина занимает минуты.
DECIDE_AFTER = dt.timedelta(minutes=30)
# Насколько цена должна уйти вверх, чтобы считать вход кошелька удачным.
WIN_PCT = 30
# Меньше решённых сделок — не статистика. Цифра совпадает с порогом остальных
# сравнений бота не случайно: доверие к кошельку стоит денег.
MIN_DECIDED = 5


@dataclass(frozen=True, slots=True)
class WalletBuy:
    """Покупка токена чужим кошельком, увиденная в логах пула."""

    wallet: str
    token: str
    pair: str
    price: Decimal
    native_wei: int


def buyer_of(entry: dict) -> str:
    """Кошелёк-получатель из лога Swap: у V2 это `to`, у V3 — `recipient`.

    Оба стоят вторым индексированным полем, поэтому разбор общий. Первое —
    роутер, и следить за ним бессмысленно: через него идут все.
    """
    topics = entry.get("topics") or []
    if len(topics) < 3:
        return ""
    raw = topics[2]
    text = raw.hex() if hasattr(raw, "hex") else str(raw)
    text = text.removeprefix("0x")
    if len(text) < 40:
        return ""
    try:
        return to_checksum("0x" + text[-40:])
    except Exception:  # noqa: BLE001 - мусор в логе не должен ронять разбор
        return ""


def wallet_buys(logs: list[dict], pools: dict[str, dict], *, native_decimals: int = 18,
                minimum: Decimal = MIN_BUY_NATIVE) -> list[WalletBuy]:
    """Чьи покупки видно в этих логах. Продажи и мелочь пропускаем.

    Один кошелёк за один проход учитывается по токену один раз: серия свапов
    внутри одной транзакции — это одно решение, а не десять.
    """
    found: dict[tuple[str, str], WalletBuy] = {}
    scale = Decimal(10) ** native_decimals
    for entry in logs:
        pair = str(entry.get("address", "")).lower()
        meta = pools.get(pair)
        if meta is None:
            continue
        parsed = parse_swap(entry, token_is_token0(meta["token"], meta["quote"]),
                            meta.get("kind", "v2"))
        if parsed is None:
            continue
        side, native_amount, token_amount = parsed
        if side != "buy" or not token_amount:
            continue
        if Decimal(native_amount) / scale < minimum:
            continue
        wallet = buyer_of(entry)
        if not wallet:
            continue
        key = (wallet.lower(), meta["token"].lower())
        if key in found:
            continue
        found[key] = WalletBuy(
            wallet=wallet, token=meta["token"], pair=pair,
            price=Decimal(native_amount) / Decimal(token_amount),
            native_wei=native_amount,
        )
    return list(found.values())


@dataclass(slots=True)
class WalletScore:
    """Итог по кошельку: сколько его входов уже можно судить и сколько удачных."""

    address: str
    decided: int = 0
    wins: int = 0
    pending: int = 0

    @property
    def win_rate(self) -> int:
        return round(self.wins * 100 / self.decided) if self.decided else 0

    def qualifies(self, min_decided: int = MIN_DECIDED, min_win_pct: int = 60) -> bool:
        """Можно ли повторять за этим кошельком."""
        return self.decided >= min_decided and self.win_rate >= min_win_pct


def decided(trade, now: dt.datetime | None = None) -> bool:
    """Прошло ли достаточно времени, чтобы судить о покупке."""
    created = trade.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return (now or dt.datetime.now(dt.UTC)) - created >= DECIDE_AFTER


def is_win(trade, win_pct: int = WIN_PCT) -> bool:
    """Ушла ли цена вверх настолько, чтобы вход считался удачным."""
    if not trade.price or trade.price <= 0 or not trade.peak_after:
        return False
    return trade.peak_after >= trade.price * (1 + Decimal(win_pct) / 100)


def score_wallets(trades: list, *, win_pct: int = WIN_PCT,
                  now: dt.datetime | None = None) -> list[WalletScore]:
    """Сводит покупки по кошелькам. Самые результативные — первыми."""
    scores: dict[str, WalletScore] = {}
    for trade in trades:
        score = scores.setdefault(trade.wallet.lower(), WalletScore(address=trade.wallet))
        if not decided(trade, now):
            score.pending += 1
            continue
        score.decided += 1
        if is_win(trade, win_pct):
            score.wins += 1
    return sorted(scores.values(), key=lambda item: (item.win_rate, item.decided), reverse=True)


def trusted(scores: list[WalletScore], *, min_decided: int = MIN_DECIDED,
            min_win_pct: int = 60) -> set[str]:
    """Адреса, за которыми бот готов повторять, в нижнем регистре."""
    return {score.address.lower() for score in scores
            if score.qualifies(min_decided, min_win_pct)}
