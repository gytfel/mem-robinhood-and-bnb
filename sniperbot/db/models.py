"""Модели БД (SQLAlchemy 2.x, async)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Wei(TypeDecorator):
    """uint256 хранится строкой: значения токенов легко выходят за int64."""

    impl = String(80)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, D102
        if value is None:
            return None
        return str(int(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001, D102
        if value is None or value == "":
            return 0
        return int(value)


class Dec(TypeDecorator):
    """Decimal без потери точности (SQLite не умеет NUMERIC как надо)."""

    impl = String(64)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, D102
        if value is None:
            return None
        return format(Decimal(str(value)), "f")

    def process_result_value(self, value, dialect):  # noqa: ANN001, D102
        if value is None or value == "":
            return None
        return Decimal(value)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # telegram id
    username: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str] = mapped_column(String(8), default="ru")

    wallet_address: Mapped[str | None] = mapped_column(String(42), index=True)
    encrypted_key: Mapped[str | None] = mapped_column(Text)
    key_fingerprint: Mapped[str | None] = mapped_column(String(32))

    active_chain: Mapped[str] = mapped_column(String(32), default="bsc")
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_deposits: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)      # бумажная торговля
    notify_level: Mapped[str] = mapped_column(String(16), default="all")
    notify_restart: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    settings: Mapped[list[ChainSettings]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class ChainSettings(Base):
    """Настройки торговли пользователя в конкретной сети."""

    __tablename__ = "chain_settings"
    __table_args__ = (UniqueConstraint("user_id", "chain", name="uq_settings_user_chain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chain: Mapped[str] = mapped_column(String(32))

    # --- покупка ---
    buy_amount: Mapped[Decimal] = mapped_column(Dec, default=Decimal("0.01"))
    slippage_bps: Mapped[int] = mapped_column(Integer, default=1500)          # 15%
    gas_multiplier_bps: Mapped[int] = mapped_column(Integer, default=12000)   # x1.2 к базовой цене газа
    gas_limit: Mapped[int] = mapped_column(Integer, default=600_000)
    approve_max: Mapped[bool] = mapped_column(Boolean, default=True)
    gas_mode: Mapped[str] = mapped_column(String(16), default="normal")   # normal|fast|turbo|manual
    priority_fee_gwei: Mapped[Decimal] = mapped_column(Dec, default=Decimal("1"))
    dex_route: Mapped[str] = mapped_column(String(8), default="auto")     # auto|v2|v3

    # --- автоснайп ---
    auto_snipe: Mapped[bool] = mapped_column(Boolean, default=False)
    max_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_snipes_per_hour: Mapped[int] = mapped_column(Integer, default=10)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    daily_loss_limit: Mapped[Decimal] = mapped_column(Dec, default=Decimal(0))
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    risk_reset_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    # --- перехват разгона (покупка уже торгующихся токенов) ---
    momentum_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    momentum_min_gain_pct: Mapped[int] = mapped_column(Integer, default=8)     # рост за окно
    momentum_max_gain_pct: Mapped[int] = mapped_column(Integer, default=80)    # выше — уже вершина
    momentum_min_trades: Mapped[int] = mapped_column(Integer, default=8)       # сделок за окно
    momentum_min_buy_ratio_pct: Mapped[int] = mapped_column(Integer, default=60)
    momentum_max_age_hours: Mapped[int] = mapped_column(Integer, default=48)   # глубина списка наблюдения
    momentum_min_volume: Mapped[Decimal] = mapped_column(Dec, default=Decimal("0.3"))

    # --- фильтры безопасности ---
    min_liquidity: Mapped[Decimal] = mapped_column(Dec, default=Decimal("2"))
    max_liquidity: Mapped[Decimal] = mapped_column(Dec, default=Decimal("0"))  # 0 = без ограничения
    max_buy_tax_bps: Mapped[int] = mapped_column(Integer, default=1000)        # 10%
    max_sell_tax_bps: Mapped[int] = mapped_column(Integer, default=1000)
    require_simulation: Mapped[bool] = mapped_column(Boolean, default=True)
    require_renounced: Mapped[bool] = mapped_column(Boolean, default=False)
    min_lp_burned_pct: Mapped[int] = mapped_column(Integer, default=0)
    honeypot_check: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- статические проверки контракта ---
    max_owner_share_pct: Mapped[int] = mapped_column(Integer, default=15)
    min_pool_share_pct: Mapped[int] = mapped_column(Integer, default=0)
    block_mintable: Mapped[bool] = mapped_column(Boolean, default=True)
    block_blacklist_fn: Mapped[bool] = mapped_column(Boolean, default=True)
    block_pausable: Mapped[bool] = mapped_column(Boolean, default=False)
    block_proxy: Mapped[bool] = mapped_column(Boolean, default=True)
    avoid_bad_creators: Mapped[bool] = mapped_column(Boolean, default=True)
    min_edge_pct: Mapped[int] = mapped_column(Integer, default=0)

    # --- автопродажа ---
    auto_sell: Mapped[bool] = mapped_column(Boolean, default=True)
    take_profit_pct: Mapped[int] = mapped_column(Integer, default=100)   # +100% => x2
    stop_loss_pct: Mapped[int] = mapped_column(Integer, default=50)      # -50%
    trailing_stop_pct: Mapped[int] = mapped_column(Integer, default=0)   # 0 = выключен
    sell_percent: Mapped[int] = mapped_column(Integer, default=100)      # доля позиции при TP
    tp_ladder: Mapped[str] = mapped_column(String(64), default="")       # «100:50,300:30»
    breakeven_pct: Mapped[int] = mapped_column(Integer, default=50)      # стоп в безубыток после +N%
    rug_guard_pct: Mapped[int] = mapped_column(Integer, default=50)      # выход при сливе ликвидности
    dead_timeout_min: Mapped[int] = mapped_column(Integer, default=0)    # выход из «мёртвой» позиции
    dead_min_pct: Mapped[int] = mapped_column(Integer, default=20)
    exit_gas_boost_bps: Mapped[int] = mapped_column(Integer, default=15_000)
    exit_slippage_bps: Mapped[int] = mapped_column(Integer, default=3_000)
    pre_approve: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- A/B-тест настроек ---
    ab_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ab_variant: Mapped[str] = mapped_column(Text, default="")   # JSON с изменёнными настройками

    # --- служебное ---
    last_native_balance: Mapped[int] = mapped_column(Wei, default=0)
    deposit_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="settings")

    @property
    def slippage_pct(self) -> Decimal:
        return Decimal(self.slippage_bps) / 100

    @property
    def gas_multiplier(self) -> Decimal:
        return Decimal(self.gas_multiplier_bps) / 10_000


class Position(Base):
    """Открытая или закрытая позиция по токену."""

    __tablename__ = "positions"
    __table_args__ = (
        Index("ix_positions_user_status", "user_id", "status"),
        Index("ix_positions_chain_status", "chain", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    chain: Mapped[str] = mapped_column(String(32))

    token_address: Mapped[str] = mapped_column(String(42), index=True)
    token_symbol: Mapped[str] = mapped_column(String(32), default="?")
    token_decimals: Mapped[int] = mapped_column(Integer, default=18)
    pair_address: Mapped[str | None] = mapped_column(String(42))
    router_address: Mapped[str] = mapped_column(String(42))
    dex_kind: Mapped[str] = mapped_column(String(8), default="v2")   # v2 | v3
    pool_fee: Mapped[int] = mapped_column(Integer, default=0)        # тир комиссии V3

    amount_wei: Mapped[int] = mapped_column(Wei, default=0)           # текущий остаток токенов
    bought_wei: Mapped[int] = mapped_column(Wei, default=0)           # сколько куплено всего
    native_spent_wei: Mapped[int] = mapped_column(Wei, default=0)
    native_returned_wei: Mapped[int] = mapped_column(Wei, default=0)

    entry_price: Mapped[Decimal | None] = mapped_column(Dec)          # native за 1 токен
    peak_price: Mapped[Decimal | None] = mapped_column(Dec)
    last_price: Mapped[Decimal | None] = mapped_column(Dec)

    status: Mapped[str] = mapped_column(String(16), default="open")   # open | closed | failed
    source: Mapped[str] = mapped_column(String(16), default="manual") # manual | auto
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False)    # сделка в тестовом режиме
    ab_group: Mapped[str] = mapped_column(String(1), default="")      # A | B при включённом тесте
    buy_tx: Mapped[str | None] = mapped_column(String(80))
    sell_tx: Mapped[str | None] = mapped_column(String(80))
    exit_reason: Mapped[str] = mapped_column(String(24), default="")   # почему закрыли позицию
    error: Mapped[str | None] = mapped_column(Text)

    # снимок правил выхода на момент покупки
    take_profit_pct: Mapped[int] = mapped_column(Integer, default=0)
    stop_loss_pct: Mapped[int] = mapped_column(Integer, default=0)
    trailing_stop_pct: Mapped[int] = mapped_column(Integer, default=0)
    auto_sell: Mapped[bool] = mapped_column(Boolean, default=True)
    sell_percent: Mapped[int] = mapped_column(Integer, default=100)
    tp_ladder: Mapped[str] = mapped_column(String(64), default="")
    tp_done: Mapped[str] = mapped_column(String(64), default="")          # сработавшие ступени
    breakeven_pct: Mapped[int] = mapped_column(Integer, default=0)
    breakeven_armed: Mapped[bool] = mapped_column(Boolean, default=False)
    rug_guard_pct: Mapped[int] = mapped_column(Integer, default=0)
    dead_timeout_min: Mapped[int] = mapped_column(Integer, default=0)
    dead_min_pct: Mapped[int] = mapped_column(Integer, default=0)
    peak_liquidity_wei: Mapped[int] = mapped_column(Wei, default=0)
    token_owner: Mapped[str | None] = mapped_column(String(42))           # для репутации создателя

    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def pnl_native_wei(self) -> int:
        return self.native_returned_wei - self.native_spent_wei

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class TradeLog(Base):
    __tablename__ = "trade_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, index=True)
    chain: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))            # buy | sell | approve | withdraw
    token_address: Mapped[str | None] = mapped_column(String(42))
    amount_in_wei: Mapped[int] = mapped_column(Wei, default=0)
    amount_out_wei: Mapped[int] = mapped_column(Wei, default=0)
    tx_hash: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|success|failed
    gas_used: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SeenPair(Base):
    """Пары, которые сканер уже видел — чтобы не обрабатывать дважды."""

    __tablename__ = "seen_pairs"
    __table_args__ = (UniqueConstraint("chain", "pair_address", name="uq_seen_chain_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32), index=True)
    pair_address: Mapped[str] = mapped_column(String(42))
    token_address: Mapped[str] = mapped_column(String(42), index=True)
    router_address: Mapped[str | None] = mapped_column(String(42))
    dex_kind: Mapped[str] = mapped_column(String(8), default="v2")
    pool_fee: Mapped[int] = mapped_column(Integer, default=0)
    block_number: Mapped[int] = mapped_column(BigInteger, default=0)
    token_symbol: Mapped[str] = mapped_column(String(32), default="")
    token_name: Mapped[str] = mapped_column(String(64), default="")
    token_owner: Mapped[str | None] = mapped_column(String(42))
    first_block_swaps: Mapped[int] = mapped_column(Integer, default=-1)  # -1 = не измеряли
    analysis_ms: Mapped[int] = mapped_column(Integer, default=0)         # сколько заняла проверка
    status: Mapped[str] = mapped_column(String(16), default="new")
    # new | waiting (ждём ликвидность) | checking | sniped | rejected
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PoolSample(Base):
    """Замер активности пула: цена и поток сделок на конец окна наблюдения.

    Разгон виден только в сравнении, поэтому одну точку хранить бессмысленно —
    сравниваем текущее окно с предыдущим замером того же пула.
    """

    __tablename__ = "pool_samples"
    __table_args__ = (Index("ix_pool_samples_chain_pool", "chain", "pool_address", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32))
    pool_address: Mapped[str] = mapped_column(String(42))
    token_address: Mapped[str] = mapped_column(String(42), default="")
    price: Mapped[Decimal | None] = mapped_column(Dec)      # нативная монета за сырую единицу токена
    swaps: Mapped[int] = mapped_column(Integer, default=0)
    buys: Mapped[int] = mapped_column(Integer, default=0)
    sells: Mapped[int] = mapped_column(Integer, default=0)
    volume_wei: Mapped[int] = mapped_column(Wei, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WalletEvent(Base):
    """Пополнения и выводы кошелька пользователя."""

    __tablename__ = "wallet_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chain: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # deposit | withdraw
    amount_wei: Mapped[int] = mapped_column(Wei, default=0)
    balance_after_wei: Mapped[int] = mapped_column(Wei, default=0)
    tx_hash: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TokenFlag(Base):
    """Чёрный/белый список токенов. user_id = NULL — глобальный список."""

    __tablename__ = "token_flags"
    __table_args__ = (UniqueConstraint("user_id", "chain", "token_address", name="uq_flag_user_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chain: Mapped[str] = mapped_column(String(32))
    token_address: Mapped[str] = mapped_column(String(42), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="blacklist")  # blacklist | whitelist
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ScannerState(Base):
    """Последний обработанный блок сканера по каждой сети/фабрике."""

    __tablename__ = "scanner_state"
    __table_args__ = (UniqueConstraint("chain", "factory", name="uq_scanner_chain_factory"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32))
    factory: Mapped[str] = mapped_column(String(42))
    last_block: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BotRun(Base):
    """История запусков бота — чтобы понимать, обновился он или просто упал."""

    __tablename__ = "bot_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    stopped_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[str] = mapped_column(String(32), default="")
    commit: Mapped[str] = mapped_column(String(32), default="")
    branch: Mapped[str] = mapped_column(String(64), default="")
    build_date: Mapped[str] = mapped_column(String(32), default="")
    clean_shutdown: Mapped[bool] = mapped_column(Boolean, default=False)
    host: Mapped[str] = mapped_column(String(64), default="")
