"""Комиссии сервиса и реферальная программа.

Две комиссии, и обе устроены по-разному:

* **за пополнение** — процент от суммы прихода, снимается один раз. Её отменяет
  реферальная программа: пригласил нужное число друзей — платить перестал;
* **за прибыль** — процент от того, что сделка заработала сверх вложенного.
  Убыточная сделка не облагается ничем: брать процент с потерь нечестно.

Модуль намеренно не знает ни про базу, ни про сеть — только арифметика. Так
правила комиссий можно проверить тестами и прочитать целиком, не собирая их по
трём файлам: с деньгами пользователей неочевидность обходится дороже всего.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

BPS = 10_000


@dataclass(frozen=True, slots=True)
class FeePolicy:
    """Правила комиссий. Пустой кошелёк выключает их полностью."""

    wallet: str = ""
    deposit_bps: int = 0
    profit_bps: int = 0
    referrals_needed: int = 3
    # Комиссию, сравнимую с ценой её отправки, брать нельзя: перевод съест
    # больше, чем принесёт, и пользователь заплатит за пустую транзакцию.
    min_ratio_to_gas: int = 3

    @property
    def enabled(self) -> bool:
        return bool(self.wallet)


@dataclass(frozen=True, slots=True)
class FeeStatus:
    """Что пользователь платит прямо сейчас и почему."""

    deposit_bps: int
    profit_bps: int
    referrals: int
    needed: int
    reason: str          # почему комиссия такая

    @property
    def deposit_pct(self) -> Decimal:
        return Decimal(self.deposit_bps) / 100

    @property
    def profit_pct(self) -> Decimal:
        return Decimal(self.profit_bps) / 100

    @property
    def free_deposit(self) -> bool:
        return self.deposit_bps == 0

    @property
    def left_to_free(self) -> int:
        return max(0, self.needed - self.referrals)


def deposit_exempt(*, referrals: int, is_admin: bool, exempt: bool,
                   policy: FeePolicy) -> tuple[bool, str]:
    """Освобождён ли пользователь от комиссии за пополнение и по какой причине."""
    if not policy.enabled or policy.deposit_bps <= 0:
        return True, "комиссия за пополнение отключена"
    if is_admin:
        return True, "вы администратор"
    if exempt:
        return True, "освобождение выдано вручную"
    if referrals >= policy.referrals_needed:
        return True, f"приглашено {referrals} — комиссия снята навсегда"
    return False, f"приглашено {referrals} из {policy.referrals_needed}"


def status_for(*, referrals: int, is_admin: bool, exempt: bool, policy: FeePolicy) -> FeeStatus:
    free, reason = deposit_exempt(referrals=referrals, is_admin=is_admin,
                                  exempt=exempt, policy=policy)
    profit_bps = 0 if (is_admin or exempt or not policy.enabled) else policy.profit_bps
    return FeeStatus(
        deposit_bps=0 if free else policy.deposit_bps,
        profit_bps=profit_bps,
        referrals=referrals,
        needed=policy.referrals_needed,
        reason=reason,
    )


def deposit_fee(amount_wei: int, *, referrals: int, is_admin: bool, exempt: bool,
                policy: FeePolicy, gas_cost_wei: int = 0) -> int:
    """Комиссия с суммы пополнения в wei. 0 — брать не нужно."""
    if amount_wei <= 0:
        return 0
    free, _ = deposit_exempt(referrals=referrals, is_admin=is_admin, exempt=exempt, policy=policy)
    if free:
        return 0
    fee = amount_wei * policy.deposit_bps // BPS
    return fee if _worth_taking(fee, gas_cost_wei, policy) else 0


def profit_fee(spent_wei: int, returned_wei: int, *, is_admin: bool, exempt: bool,
               policy: FeePolicy, gas_cost_wei: int = 0) -> int:
    """Комиссия с прибыли закрытой сделки. Убыток комиссией не облагается."""
    if not policy.enabled or policy.profit_bps <= 0 or is_admin or exempt:
        return 0
    profit = returned_wei - spent_wei
    if profit <= 0:
        return 0
    fee = profit * policy.profit_bps // BPS
    return fee if _worth_taking(fee, gas_cost_wei, policy) else 0


def _worth_taking(fee_wei: int, gas_cost_wei: int, policy: FeePolicy) -> bool:
    """Стоит ли отправлять такую комиссию отдельной транзакцией."""
    if fee_wei <= 0:
        return False
    if gas_cost_wei <= 0:
        return True
    return fee_wei >= gas_cost_wei * policy.min_ratio_to_gas


def referral_link(bot_username: str, user_id: int) -> str:
    """Ссылка-приглашение: Telegram отдаст payload боту при /start."""
    name = (bot_username or "").lstrip("@")
    return f"https://t.me/{name}?start=ref{user_id}" if name else ""


def parse_referral(payload: str | None) -> int | None:
    """Разбирает payload из /start. Чужой формат молча игнорируется."""
    text = (payload or "").strip()
    if not text.lower().startswith("ref"):
        return None
    digits = text[3:]
    return int(digits) if digits.isdigit() else None
