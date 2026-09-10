"""Конфигурация бота: .env + config/chains.json."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CHAINS_FILE = ROOT_DIR / "config" / "chains.json"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _split(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


# Тиры комиссий Uniswap V3 по умолчанию (форки добавляют свои, например 2500).
DEFAULT_V3_FEES = (100, 500, 3000, 10000)


@dataclass(slots=True)
class RouterConfig:
    """DEX: Uniswap V2-совместимый роутер либо Uniswap V3 (SwapRouter + Quoter)."""

    name: str
    router: str
    factory: str
    fee_bps: int = 30
    default: bool = False
    kind: str = "v2"                     # v2 | v3
    quoter: str = ""                     # только v3: QuoterV2 (или Quoter v1)
    fee_tiers: tuple[int, ...] = DEFAULT_V3_FEES
    variant: str = "auto"                # v3: auto | router02 | router01

    @property
    def is_v3(self) -> bool:
        return self.kind == "v3"

    @property
    def configured(self) -> bool:
        if not (self.router and self.factory):
            return False
        # Для V3 нужен ещё Quoter: без него не получить котировку до сделки.
        return bool(self.quoter) if self.is_v3 else True

    @property
    def missing(self) -> list[str]:
        gaps = []
        if not self.router:
            gaps.append("ROUTER")
        if not self.factory:
            gaps.append("FACTORY")
        if self.is_v3 and not self.quoter:
            gaps.append("QUOTER")
        return gaps


@dataclass(slots=True)
class ChainConfig:
    """Описание EVM-сети. Всё, что нужно движку, лежит здесь."""

    key: str
    name: str
    chain_id: int
    native_symbol: str = "ETH"
    native_decimals: int = 18
    enabled: bool = False
    poa: bool = True
    eip1559: bool = False
    block_time: float = 3.0
    rpc_urls: list[str] = field(default_factory=list)
    explorer_url: str = ""
    wrapped_native: str = ""
    stable_token: str = ""
    routers: list[RouterConfig] = field(default_factory=list)
    lp_burn_addresses: list[str] = field(default_factory=list)

    @property
    def default_router(self) -> RouterConfig | None:
        for router in self.routers:
            if router.default and router.configured:
                return router
        for router in self.routers:
            if router.configured:
                return router
        return None

    @property
    def active_routers(self) -> list[RouterConfig]:
        """Все настроенные DEX сети — по ним работают сканер и поиск пула."""
        return [router for router in self.routers if router.configured]

    @property
    def missing(self) -> list[str]:
        """Чего не хватает сети для работы — списком, для понятных подсказок."""
        gaps: list[str] = []
        if not self.rpc_urls:
            gaps.append("RPC_URLS")
        if not self.chain_id:
            gaps.append("CHAIN_ID")
        if not self.wrapped_native:
            gaps.append("WRAPPED_NATIVE")
        if self.default_router is None:
            # Подойдёт любая площадка: V2 (роутер + фабрика) либо V3 (+ Quoter).
            gaps.append("ROUTER+FACTORY (V2) или V3_ROUTER+V3_FACTORY+V3_QUOTER (V3)")
        return gaps

    @property
    def configured(self) -> bool:
        """Готова ли сеть к работе (есть RPC, chain_id, WNATIVE и роутер)."""
        return not self.missing

    def router_by_address(self, address: str) -> RouterConfig | None:
        address = (address or "").lower()
        for router in self.routers:
            if router.router.lower() == address:
                return router
        return None

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer_url}/tx/{tx_hash}" if self.explorer_url else tx_hash

    def token_url(self, address: str) -> str:
        return f"{self.explorer_url}/token/{address}" if self.explorer_url else address

    def address_url(self, address: str) -> str:
        return f"{self.explorer_url}/address/{address}" if self.explorer_url else address


class Settings(BaseSettings):
    """Значения из .env (переменные окружения имеют приоритет)."""

    model_config = SettingsConfigDict(
        env_file=os.getenv("SNIPER_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    allowed_user_ids_raw: str = Field(default="", alias="ALLOWED_USER_IDS")

    master_key: str = Field(default="", alias="MASTER_KEY")

    database_url: str = Field(default="sqlite+aiosqlite:///data/sniper.db", alias="DATABASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    chains_file: str = Field(default=str(DEFAULT_CHAINS_FILE), alias="CHAINS_FILE")
    enabled_chains_raw: str = Field(default="", alias="ENABLED_CHAINS")
    default_chain: str = Field(default="bsc", alias="DEFAULT_CHAIN")

    scanner_poll_interval: float = Field(default=2.0, alias="SCANNER_POLL_INTERVAL")
    scanner_liquidity_wait_blocks: int = Field(default=40, alias="SCANNER_LIQUIDITY_WAIT_BLOCKS")
    scanner_liquidity_watch_minutes: int = Field(default=180, alias="SCANNER_LIQUIDITY_WATCH_MINUTES")
    scanner_watch_interval: float = Field(default=12.0, alias="SCANNER_WATCH_INTERVAL")
    position_poll_interval: float = Field(default=6.0, alias="POSITION_POLL_INTERVAL")
    fast_poll_interval: float = Field(default=1.5, alias="FAST_POLL_INTERVAL")
    fast_watch_minutes: float = Field(default=15.0, alias="FAST_WATCH_MINUTES")
    deposit_poll_interval: float = Field(default=30.0, alias="DEPOSIT_POLL_INTERVAL")

    # перехват разгона: как часто снимать замеры и сколько пулов держать под наблюдением
    momentum_interval: float = Field(default=45.0, alias="MOMENTUM_INTERVAL")
    momentum_watch_limit: int = Field(default=300, alias="MOMENTUM_WATCH_LIMIT")
    momentum_sample_ttl_hours: int = Field(default=24, alias="MOMENTUM_SAMPLE_TTL_HOURS")
    momentum_retry_minutes: int = Field(default=30, alias="MOMENTUM_RETRY_MINUTES")
    # разовый разбор истории фабрик при старте: наполняет список наблюдения
    # пулами, которые появились до запуска бота. 0 — не разбирать
    momentum_backfill_blocks: int = Field(default=20_000, alias="MOMENTUM_BACKFILL_BLOCKS")

    # сколько ещё ждать подтверждения покупки, если сеть не уложилась в 180 с.
    # Транзакция уже отправлена, поэтому бросать её нельзя — токены придут позже
    pending_buy_timeout: float = Field(default=1800.0, alias="PENDING_BUY_TIMEOUT")

    service_fee_bps: int = Field(default=0, alias="SERVICE_FEE_BPS")
    service_fee_wallet: str = Field(default="", alias="SERVICE_FEE_WALLET")

    @field_validator("service_fee_bps")
    @classmethod
    def _limit_fee(cls, value: int) -> int:
        if value < 0 or value > 500:
            raise ValueError("SERVICE_FEE_BPS должен быть в диапазоне 0..500 (0-5%)")
        return value

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in _split(self.admin_ids_raw) if x.lstrip("-").isdigit()}

    @property
    def allowed_user_ids(self) -> set[int]:
        return {int(x) for x in _split(self.allowed_user_ids_raw) if x.lstrip("-").isdigit()}

    @property
    def enabled_chains(self) -> list[str]:
        return [c.lower() for c in _split(self.enabled_chains_raw)]

    @property
    def resolved_database_url(self) -> str:
        """URL базы с абсолютным путём.

        Относительный путь в DATABASE_URL считается от каталога с .env, а не от
        текущего каталога: иначе запуск из другого места создал бы вторую пустую
        базу, и кошельки пользователей «пропали» бы.
        """
        marker = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(marker):
            return self.database_url
        raw = self.database_url[len(marker) :]
        if not raw or raw == ":memory:" or raw.startswith("/"):
            return self.database_url
        base = Path(os.getenv("SNIPER_ENV_FILE", ".env")).resolve().parent
        return marker + str((base / raw).resolve())

    @property
    def service_fee_rate(self) -> Decimal:
        return Decimal(self.service_fee_bps) / Decimal(10_000)

    def validate_runtime(self) -> list[str]:
        """Проверки, без которых бот не должен стартовать."""
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан (получите токен у @BotFather)")
        if len(self.master_key) < 16:
            problems.append("MASTER_KEY не задан или слишком короткий (нужно ≥16 символов)")
        if self.master_key.startswith("CHANGE_ME"):
            problems.append("MASTER_KEY оставлен из примера — замените на случайную строку")
        if self.service_fee_bps and not self.service_fee_wallet:
            problems.append("SERVICE_FEE_BPS > 0, но SERVICE_FEE_WALLET не задан")
        return problems


# Короткие префиксы env-переменных для сетей: BSC_RPC_URLS, RH_ROUTER и т.д.
ENV_PREFIXES = {"bsc": "BSC", "robinhood": "RH"}


def env_prefix(chain_key: str) -> str:
    return ENV_PREFIXES.get(chain_key, chain_key.upper())


def env_values() -> dict[str, str]:
    """Значения из .env плюс переменные окружения (окружение приоритетнее).

    Настройки сетей не объявлены полями Settings, поэтому pydantic их не читает —
    файл .env приходится разбирать самостоятельно, иначе RH_ROUTER и подобные
    работали бы только через export в шелле.
    """
    path = Path(os.getenv("SNIPER_ENV_FILE", ".env"))
    values: dict[str, str] = {}
    if path.exists():
        values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    values.update(os.environ)
    return values


def _apply_env_overrides(key: str, raw: dict, source: dict[str, str] | None = None) -> dict:
    """Позволяет переопределить параметры сети через .env или переменные окружения."""
    prefix = env_prefix(key)
    source = env_values() if source is None else source

    def env(name: str) -> str | None:
        value = source.get(f"{prefix}_{name}")
        return value.strip() if value and value.strip() else None

    if (rpc := env("RPC_URLS")) is not None:
        raw["rpc_urls"] = _split(rpc)
    if (chain_id := env("CHAIN_ID")) is not None and chain_id.isdigit():
        raw["chain_id"] = int(chain_id)
    if (wrapped := env("WRAPPED_NATIVE")) is not None:
        raw["wrapped_native"] = wrapped
    if (explorer := env("EXPLORER_URL")) is not None:
        raw["explorer_url"] = explorer.rstrip("/")
    if (symbol := env("NATIVE_SYMBOL")) is not None:
        raw["native_symbol"] = symbol
    if (stable := env("STABLE_TOKEN")) is not None:
        raw["stable_token"] = stable
    if (enabled := env("ENABLED")) is not None:
        raw["enabled"] = enabled.lower() in {"1", "true", "yes", "on"}

    routers = list(raw.get("routers") or [])

    def _slot(kind: str, name: str) -> dict:
        """Находит (или создаёт) описание роутера нужного типа."""
        for item in routers:
            if str(item.get("kind", "v2")).lower() == kind:
                return item
        item = {"name": name, "kind": kind, "default": kind == "v2"}
        routers.append(item)
        return item

    router_addr, factory_addr = env("ROUTER"), env("FACTORY")
    if router_addr or factory_addr:
        primary = _slot("v2", f"{key} DEX V2")
        if router_addr:
            primary["router"] = router_addr
        if factory_addr:
            primary["factory"] = factory_addr

    v3_router, v3_factory, v3_quoter = env("V3_ROUTER"), env("V3_FACTORY"), env("V3_QUOTER")
    if v3_router or v3_factory or v3_quoter:
        pool = _slot("v3", f"{key} DEX V3")
        if v3_router:
            pool["router"] = v3_router
        if v3_factory:
            pool["factory"] = v3_factory
        if v3_quoter:
            pool["quoter"] = v3_quoter
    if (fees := env("V3_FEES")) is not None:
        _slot("v3", f"{key} DEX V3")["fee_tiers"] = [int(f) for f in _split(fees) if f.isdigit()]
    if (variant := env("V3_VARIANT")) is not None:
        _slot("v3", f"{key} DEX V3")["variant"] = variant.lower()

    if (default_kind := env("DEFAULT_DEX")) is not None:
        wanted = default_kind.lower()
        for item in routers:
            item["default"] = str(item.get("kind", "v2")).lower() == wanted

    if routers:
        raw["routers"] = routers
    return raw


def load_chains(path: str | Path | None = None, settings: Settings | None = None) -> dict[str, ChainConfig]:
    """Читает config/chains.json, применяет env-оверрайды и фильтр ENABLED_CHAINS."""
    settings = settings or get_settings()
    path = Path(path or settings.chains_file)
    if not path.exists():
        raise FileNotFoundError(f"Файл конфигурации сетей не найден: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    wanted = set(settings.enabled_chains)
    source = env_values()
    chains: dict[str, ChainConfig] = {}

    for key, raw in data.items():
        if key.startswith("_"):
            continue
        raw = _apply_env_overrides(key, dict(raw), source)
        routers = [
            RouterConfig(
                name=r.get("name", "DEX"),
                router=r.get("router", ""),
                factory=r.get("factory", ""),
                fee_bps=int(r.get("fee_bps", 30)),
                default=bool(r.get("default", False)),
                kind=str(r.get("kind", "v2")).lower(),
                quoter=r.get("quoter", ""),
                fee_tiers=tuple(int(f) for f in r.get("fee_tiers", DEFAULT_V3_FEES)),
                variant=str(r.get("variant", "auto")).lower(),
            )
            for r in raw.get("routers", [])
        ]
        chain = ChainConfig(
            key=key,
            name=raw.get("name", key),
            chain_id=int(raw.get("chain_id", 0)),
            native_symbol=raw.get("native_symbol", "ETH"),
            native_decimals=int(raw.get("native_decimals", 18)),
            enabled=bool(raw.get("enabled", False)),
            poa=bool(raw.get("poa", True)),
            eip1559=bool(raw.get("eip1559", False)),
            block_time=float(raw.get("block_time", 3.0)),
            rpc_urls=list(raw.get("rpc_urls", [])),
            explorer_url=str(raw.get("explorer_url", "")).rstrip("/"),
            wrapped_native=raw.get("wrapped_native", ""),
            stable_token=raw.get("stable_token", ""),
            routers=routers,
            lp_burn_addresses=list(raw.get("lp_burn_addresses", [ZERO_ADDRESS])),
        )
        if wanted:
            chain.enabled = key in wanted
        if chain.enabled and not chain.configured:
            # Сеть включена, но не дозаполнена — не запускаем её, а не падаем целиком.
            chain.enabled = False
        chains[key] = chain

    return chains


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def get_chains() -> dict[str, ChainConfig]:
    return load_chains()


def active_chains() -> dict[str, ChainConfig]:
    return {key: chain for key, chain in get_chains().items() if chain.enabled}


def get_chain(key: str) -> ChainConfig:
    chains = get_chains()
    if key not in chains:
        raise KeyError(f"Неизвестная сеть: {key}")
    return chains[key]
