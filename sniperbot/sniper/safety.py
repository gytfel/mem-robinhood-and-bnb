"""Анти-honeypot и анти-rug проверки токена.

Ключевая идея: не доверять «глазам», а симулировать реальные покупку и продажу
через `eth_call` с state override.

* покупка — вызов `swapExactETHForTokensSupportingFeeOnTransferTokens`
  от имени случайного адреса с подменённым балансом;
* продажа — тот же роутер, но токены и allowance выдаются адресу
  через `stateDiff` (слот `balanceOf`/`allowance` ищется перебором);
* налоги — двоичный поиск по `amountOutMin`: роутер сам проверяет
  `amountOut >= amountOutMin`, поэтому максимальное проходящее значение
  и есть реально полученная сумма.

Если нода не поддерживает state override, проверки помечаются как
недоступные — решение остаётся за пользователем (`require_simulation`).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from eth_utils import to_checksum_address

from sniperbot.chain.clients import ChainClient
from sniperbot.chain.dex import (
    PairState,
    amounts_out,
    get_pair_address,
    lp_burned_pct,
    read_pair,
)
from sniperbot.chain.erc20 import TokenInfo, fetch_token, trading_limits
from sniperbot.config import RouterConfig
from sniperbot.utils.evm import hex32, mapping_slot, nested_mapping_slot
from sniperbot.utils.fmt import from_wei

log = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1
PROBE_NATIVE_BALANCE = 100 * 10**18   # 100 монет пробнику на газ и покупку
SIM_GAS = 3_000_000
SLOT_PROBE_VALUE = 0x1234567890
MAX_SLOT_SCAN = 24
BINARY_SEARCH_STEPS = 16

# Кэш найденных слотов хранилища: {(chain, token): (balance_slot, vyper, allowance_slot)}
_slot_cache: dict[tuple[str, str], tuple[int, bool, int | None]] = {}


@dataclass(slots=True)
class Check:
    key: str
    title: str
    ok: bool | None          # None = проверить не удалось
    detail: str = ""
    critical: bool = False

    @property
    def icon(self) -> str:
        if self.ok is None:
            return "❔"
        return "✅" if self.ok else ("⛔️" if self.critical else "⚠️")


@dataclass(slots=True)
class SimulationResult:
    available: bool = False
    can_buy: bool | None = None
    can_sell: bool | None = None
    buy_tax_bps: int | None = None
    sell_tax_bps: int | None = None
    error: str | None = None

    @property
    def is_honeypot(self) -> bool:
        return self.can_sell is False or self.can_buy is False


@dataclass(slots=True)
class SafetyReport:
    token: TokenInfo
    chain_key: str
    router: str
    pair: str | None = None
    pair_state: PairState | None = None
    liquidity_native: Decimal = Decimal(0)
    lp_burned: Decimal | None = None
    simulation: SimulationResult = field(default_factory=SimulationResult)
    limits: dict = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)

    @property
    def buy_tax_pct(self) -> Decimal | None:
        bps = self.simulation.buy_tax_bps
        return None if bps is None else Decimal(bps) / 100

    @property
    def sell_tax_pct(self) -> Decimal | None:
        bps = self.simulation.sell_tax_bps
        return None if bps is None else Decimal(bps) / 100

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False and c.critical]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False and not c.critical]

    @property
    def score(self) -> int:
        """Грубая оценка 0..100 — только для быстрой визуальной ориентации."""
        done = [c for c in self.checks if c.ok is not None]
        if not done:
            return 0
        weights = {True: 1.0, False: 0.0}
        total = 0.0
        weight_sum = 0.0
        for check in done:
            weight = 2.0 if check.critical else 1.0
            total += weights[bool(check.ok)] * weight
            weight_sum += weight
        return int(round(100 * total / weight_sum)) if weight_sum else 0

    @property
    def verdict(self) -> str:
        if self.blocking:
            return "danger"
        if self.warnings or self.simulation.available is False:
            return "risky"
        return "safe"

    def passes(self) -> tuple[bool, list[str]]:
        problems = [f"{c.title}: {c.detail}" if c.detail else c.title for c in self.blocking]
        return (not problems), problems


class NodeOverrideUnsupported(RuntimeError):
    """Нода не умеет eth_call со state override."""


class HoneypotSimulator:
    """Симуляция покупки/продажи токена без реальных транзакций."""

    def __init__(self, client: ChainClient, router_cfg: RouterConfig) -> None:
        self.client = client
        self.router_cfg = router_cfg
        self.router = to_checksum_address(router_cfg.router)
        self.wnative = to_checksum_address(client.config.wrapped_native)

    # ------------------------------------------------------------ утилиты
    @staticmethod
    def _probe_address() -> str:
        return to_checksum_address("0x" + secrets.token_hex(20))

    def _encode(self, fn_name: str, *args) -> str:
        contract = self.client.router(self.router)
        return contract.encode_abi(fn_name, args=list(args))

    async def _call(self, tx: dict, overrides: dict) -> bytes | None:
        """eth_call; None — если вызов зареверчен."""
        try:
            return await self.client.raw_call(tx, overrides)
        except Exception as exc:  # noqa: BLE001 - revert это ожидаемый исход
            message = str(exc).lower()
            if any(token in message for token in ("state override", "not supported", "unsupported", "-32601")):
                raise NodeOverrideUnsupported(str(exc)) from exc
            return None

    async def supports_override(self) -> bool:
        probe = self._probe_address()
        try:
            await self.client.raw_call(
                {"from": probe, "to": self.router, "data": self._encode("WETH")},
                {probe: {"balance": hex(PROBE_NATIVE_BALANCE)}},
            )
        except NodeOverrideUnsupported:
            return False
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if any(t in message for t in ("state override", "not supported", "unsupported", "-32601", "invalid argument")):
                return False
        return True

    # --------------------------------------------------- поиск слотов ERC20
    async def find_balance_slot(self, token: str, holder: str) -> tuple[int, bool] | None:
        cached = _slot_cache.get((self.client.config.key, token.lower()))
        if cached:
            return cached[0], cached[1]

        erc20 = self.client.erc20(token)
        data = erc20.encode_abi("balanceOf", args=[holder])
        tx = {"to": to_checksum_address(token), "data": data}

        async def probe(slot: int, vyper: bool) -> tuple[int, bool] | None:
            key = mapping_slot(holder, slot, vyper_layout=vyper)
            overrides = {to_checksum_address(token): {"stateDiff": {key: hex32(SLOT_PROBE_VALUE)}}}
            try:
                result = await self.client.raw_call(tx, overrides)
            except Exception:  # noqa: BLE001
                return None
            if result and int.from_bytes(result[-32:], "big") == SLOT_PROBE_VALUE:
                return slot, vyper
            return None

        for start in range(0, MAX_SLOT_SCAN, 8):
            batch = [
                probe(slot, vyper)
                for slot in range(start, min(start + 8, MAX_SLOT_SCAN))
                for vyper in (False, True)
            ]
            for found in await asyncio.gather(*batch, return_exceptions=True):
                if isinstance(found, tuple):
                    _slot_cache[(self.client.config.key, token.lower())] = (found[0], found[1], None)
                    return found
        return None

    async def find_allowance_slot(self, token: str, owner: str, spender: str) -> int | None:
        cache_key = (self.client.config.key, token.lower())
        cached = _slot_cache.get(cache_key)
        if cached and cached[2] is not None:
            return cached[2]

        erc20 = self.client.erc20(token)
        data = erc20.encode_abi("allowance", args=[owner, spender])
        tx = {"to": to_checksum_address(token), "data": data}

        async def probe(slot: int) -> int | None:
            key = nested_mapping_slot(owner, spender, slot)
            overrides = {to_checksum_address(token): {"stateDiff": {key: hex32(SLOT_PROBE_VALUE)}}}
            try:
                result = await self.client.raw_call(tx, overrides)
            except Exception:  # noqa: BLE001
                return None
            if result and int.from_bytes(result[-32:], "big") == SLOT_PROBE_VALUE:
                return slot
            return None

        for start in range(0, MAX_SLOT_SCAN, 8):
            results = await asyncio.gather(
                *(probe(slot) for slot in range(start, min(start + 8, MAX_SLOT_SCAN))),
                return_exceptions=True,
            )
            for found in results:
                if isinstance(found, int):
                    if cached:
                        _slot_cache[cache_key] = (cached[0], cached[1], found)
                    return found
        return None

    # ------------------------------------------------------------ симуляция
    async def _max_passing_min_out(self, tx: dict, overrides: dict, build, upper: int) -> int:
        """Двоичный поиск максимального amountOutMin, при котором свап проходит."""
        if upper <= 0:
            return 0
        low, high, best = 0, upper, 0
        for _ in range(BINARY_SEARCH_STEPS):
            if low > high:
                break
            mid = (low + high) // 2
            probe_tx = dict(tx)
            probe_tx["data"] = build(mid)
            if await self._call(probe_tx, overrides) is not None:
                best = mid
                low = mid + 1
            else:
                high = mid - 1
        return best

    async def simulate(self, token: str, decimals: int, amount_native_wei: int) -> SimulationResult:
        token = to_checksum_address(token)
        result = SimulationResult()
        try:
            if not await self.supports_override():
                result.error = "RPC не поддерживает state override — симуляция недоступна"
                return result
            result.available = True

            probe = self._probe_address()
            deadline = 2**32
            buy_path = [self.wnative, token]
            sell_path = [token, self.wnative]

            # --- ожидаемый выход по формуле пула ---
            try:
                expected_tokens = (await amounts_out(self.client, self.router, amount_native_wei, buy_path))[-1]
            except Exception as exc:  # noqa: BLE001
                result.error = f"Нет ликвидности для котировки: {exc}"
                result.can_buy = False
                return result
            if expected_tokens <= 0:
                result.can_buy = False
                result.error = "Пул не отдаёт токены за нативную монету"
                return result

            # --- покупка ---
            buy_tx = {
                "from": probe,
                "to": self.router,
                "value": hex(amount_native_wei),
                "gas": hex(SIM_GAS),
                "data": self._encode(
                    "swapExactETHForTokensSupportingFeeOnTransferTokens", 0, buy_path, probe, deadline
                ),
            }
            buy_overrides = {probe: {"balance": hex(PROBE_NATIVE_BALANCE)}}
            if await self._call(buy_tx, buy_overrides) is None:
                result.can_buy = False
                result.error = "Покупка не проходит (торговля закрыта или чёрный список)"
                return result
            result.can_buy = True

            received = await self._max_passing_min_out(
                buy_tx,
                buy_overrides,
                lambda min_out: self._encode(
                    "swapExactETHForTokensSupportingFeeOnTransferTokens", min_out, buy_path, probe, deadline
                ),
                expected_tokens,
            )
            result.buy_tax_bps = _tax_bps(expected_tokens, received)

            # --- продажа ---
            sell_amount = received or expected_tokens
            slot = await self.find_balance_slot(token, probe)
            if slot is None:
                result.can_sell = None
                result.error = "Не удалось найти слот баланса токена — продажа не проверена"
                return result
            balance_key = mapping_slot(probe, slot[0], vyper_layout=slot[1])
            state_diff = {balance_key: hex32(sell_amount * 2)}

            allowance_slot = await self.find_allowance_slot(token, probe, self.router)
            if allowance_slot is not None:
                state_diff[nested_mapping_slot(probe, self.router, allowance_slot)] = hex32(MAX_UINT256)

            sell_overrides = {
                probe: {"balance": hex(PROBE_NATIVE_BALANCE)},
                token: {"stateDiff": state_diff},
            }
            sell_tx = {
                "from": probe,
                "to": self.router,
                "gas": hex(SIM_GAS),
                "data": self._encode(
                    "swapExactTokensForETHSupportingFeeOnTransferTokens",
                    sell_amount, 0, sell_path, probe, deadline,
                ),
            }
            if await self._call(sell_tx, sell_overrides) is None:
                result.can_sell = False
                result.error = "Продажа не проходит — вероятный honeypot"
                return result
            result.can_sell = True

            try:
                expected_native = (await amounts_out(self.client, self.router, sell_amount, sell_path))[-1]
            except Exception:  # noqa: BLE001
                expected_native = 0
            if expected_native > 0:
                got_native = await self._max_passing_min_out(
                    sell_tx,
                    sell_overrides,
                    lambda min_out: self._encode(
                        "swapExactTokensForETHSupportingFeeOnTransferTokens",
                        sell_amount, min_out, sell_path, probe, deadline,
                    ),
                    expected_native,
                )
                result.sell_tax_bps = _tax_bps(expected_native, got_native)
        except NodeOverrideUnsupported as exc:
            result.available = False
            result.error = f"RPC не поддерживает state override: {exc}"
        except Exception as exc:  # noqa: BLE001 - симуляция не должна ронять бота
            log.warning("Симуляция %s не удалась: %s", token, exc)
            result.error = f"Симуляция не удалась: {exc}"
        return result


def _tax_bps(expected: int, actual: int) -> int:
    """Налог в базисных пунктах по разнице ожидаемого и фактического выхода.

    Двоичный поиск даёт результат с точностью 1/2^16 от суммы, поэтому
    итог округляется до 0.1% — это заведомо выше погрешности измерения.
    """
    if expected <= 0:
        return 0
    tax = (expected - actual) * 10_000 / expected
    rounded = int(round(tax / 10.0)) * 10
    return max(0, min(10_000, rounded))


async def analyze_token(
    client: ChainClient,
    router_cfg: RouterConfig,
    token_address: str,
    *,
    amount_native_wei: int,
    settings=None,
    run_simulation: bool = True,
) -> SafetyReport:
    """Полный отчёт по токену: ликвидность, налоги, honeypot, LP, владелец."""
    token = await fetch_token(client, token_address)
    report = SafetyReport(token=token, chain_key=client.config.key, router=router_cfg.router)
    checks = report.checks

    code = await client.run(lambda w3: w3.eth.get_code(to_checksum_address(token_address)))
    if not code or len(code) <= 2:
        checks.append(Check("contract", "Контракт токена", False, "по адресу нет кода", critical=True))
        return report
    checks.append(Check("contract", "Контракт токена", True, f"{len(code)} байт"))

    pair = await get_pair_address(client, router_cfg, token.address)
    report.pair = pair
    if not pair:
        checks.append(Check("pair", "Пара на DEX", False, "пара не найдена", critical=True))
        return report

    state = await read_pair(client, pair, token.address, token.decimals)
    report.pair_state = state
    report.liquidity_native = state.liquidity_native
    symbol = client.config.native_symbol

    if not state.has_liquidity:
        checks.append(Check("liquidity", "Ликвидность", False, "пул пустой", critical=True))
        return report

    min_liq = Decimal(str(getattr(settings, "min_liquidity", 0) or 0))
    max_liq = Decimal(str(getattr(settings, "max_liquidity", 0) or 0))
    liq_ok = state.liquidity_native >= min_liq if min_liq else True
    if max_liq and state.liquidity_native > max_liq:
        liq_ok = False
    checks.append(
        Check(
            "liquidity",
            "Ликвидность",
            liq_ok,
            f"{from_wei(state.reserve_native):.4f} {symbol}",
            critical=True,
        )
    )

    report.lp_burned = await lp_burned_pct(client, pair)
    min_burn = int(getattr(settings, "min_lp_burned_pct", 0) or 0)
    if report.lp_burned is not None:
        if min_burn:
            lp_ok: bool | None = report.lp_burned >= min_burn
        else:
            # Без явного требования: 50%+ сожжённого LP считаем хорошим знаком,
            # меньше — не приговор, а «неизвестно» (LP может быть в локере).
            lp_ok = True if report.lp_burned >= 50 else None
        checks.append(
            Check("lp_lock", "LP сожжён/заблокирован", lp_ok, f"{report.lp_burned:.1f}%", critical=bool(min_burn))
        )

    require_renounced = bool(getattr(settings, "require_renounced", False))
    owner_ok: bool | None = token.renounced if require_renounced else (True if token.renounced else None)
    checks.append(
        Check(
            "owner",
            "Владелец контракта",
            owner_ok,
            "renounced" if token.renounced else (token.owner or "неизвестен"),
            critical=require_renounced,
        )
    )

    report.limits = await trading_limits(client, token.address)
    if report.limits:
        checks.append(Check("limits", "Лимиты токена", None, _fmt_limits(report.limits, token.decimals)))

    if run_simulation:
        simulator = HoneypotSimulator(client, router_cfg)
        report.simulation = await simulator.simulate(token.address, token.decimals, amount_native_wei)
        _apply_simulation_checks(report, settings)

    return report


def _apply_simulation_checks(report: SafetyReport, settings) -> None:
    sim = report.simulation
    checks = report.checks
    require_sim = bool(getattr(settings, "require_simulation", True))

    if not sim.available:
        checks.append(
            Check("simulation", "Симуляция сделки", None if not require_sim else False,
                  sim.error or "недоступна", critical=require_sim)
        )
        return

    checks.append(Check("buy", "Покупка проходит", sim.can_buy, sim.error or "", critical=True))
    checks.append(
        Check(
            "sell",
            "Продажа проходит (honeypot)",
            sim.can_sell,
            "honeypot" if sim.can_sell is False else ("не проверено" if sim.can_sell is None else "ок"),
            critical=True,
        )
    )

    max_buy = int(getattr(settings, "max_buy_tax_bps", 10_000) or 10_000)
    max_sell = int(getattr(settings, "max_sell_tax_bps", 10_000) or 10_000)
    if sim.buy_tax_bps is not None:
        checks.append(
            Check("buy_tax", "Налог на покупку", sim.buy_tax_bps <= max_buy,
                  f"{sim.buy_tax_bps / 100:.1f}% (лимит {max_buy / 100:.0f}%)", critical=True)
        )
    if sim.sell_tax_bps is not None:
        checks.append(
            Check("sell_tax", "Налог на продажу", sim.sell_tax_bps <= max_sell,
                  f"{sim.sell_tax_bps / 100:.1f}% (лимит {max_sell / 100:.0f}%)", critical=True)
        )


def _fmt_limits(limits: dict, decimals: int) -> str:
    parts = []
    for key, value in limits.items():
        if isinstance(value, bool):
            parts.append(f"{key}={'да' if value else 'нет'}")
        else:
            parts.append(f"{key}={from_wei(int(value), decimals):.0f}")
    return ", ".join(parts)


def evaluate_for_settings(report: SafetyReport, cfg) -> tuple[bool, list[str]]:
    """Проверяет готовый отчёт против настроек конкретного пользователя.

    Отчёт строится один раз на пару, а фильтры у всех разные — поэтому
    сравнение вынесено отдельно от сбора данных.
    """
    reasons: list[str] = []
    sim = report.simulation

    if report.pair is None or report.pair_state is None or not report.pair_state.has_liquidity:
        return False, ["нет ликвидности"]

    min_liq = Decimal(str(getattr(cfg, "min_liquidity", 0) or 0))
    max_liq = Decimal(str(getattr(cfg, "max_liquidity", 0) or 0))
    liquidity = report.liquidity_native
    if min_liq and liquidity < min_liq:
        reasons.append(f"ликвидность {liquidity:.3f} < минимума {min_liq}")
    if max_liq and liquidity > max_liq:
        reasons.append(f"ликвидность {liquidity:.3f} > максимума {max_liq}")

    if getattr(cfg, "honeypot_check", True):
        if sim.can_sell is False or sim.can_buy is False:
            reasons.append("honeypot: сделка не проходит в симуляции")
        elif getattr(cfg, "require_simulation", True) and (not sim.available or sim.can_sell is None):
            reasons.append(sim.error or "симуляция недоступна")

    max_buy = int(getattr(cfg, "max_buy_tax_bps", 10_000) or 10_000)
    max_sell = int(getattr(cfg, "max_sell_tax_bps", 10_000) or 10_000)
    if sim.buy_tax_bps is not None and sim.buy_tax_bps > max_buy:
        reasons.append(f"налог на покупку {sim.buy_tax_bps / 100:.1f}% > {max_buy / 100:.0f}%")
    if sim.sell_tax_bps is not None and sim.sell_tax_bps > max_sell:
        reasons.append(f"налог на продажу {sim.sell_tax_bps / 100:.1f}% > {max_sell / 100:.0f}%")

    min_burn = int(getattr(cfg, "min_lp_burned_pct", 0) or 0)
    if min_burn:
        if report.lp_burned is None:
            reasons.append("не удалось проверить блокировку LP")
        elif report.lp_burned < min_burn:
            reasons.append(f"LP сожжён на {report.lp_burned:.1f}% < {min_burn}%")

    if getattr(cfg, "require_renounced", False) and not report.token.renounced:
        reasons.append("владелец контракта не отказался от прав")

    return (not reasons), reasons
