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


STATE_KEY = "fees"                  # где решение команды /fees лежит в базе
MAX_FEE_BPS = 5_000                 # 50%: выше это уже не комиссия, а опечатка


@dataclass
class FeeSettings:
    """Изменяемые настройки комиссий.

    `.env` задаёт стартовые значения, команда `/fees` — итоговое решение, и оно
    важнее файла. Флаг `off` выключает комиссии, не забывая кошелёк: включить
    обратно должно быть так же просто, как выключить.
    """

    wallet: str = ""
    deposit_bps: int = 0
    profit_bps: int = 0
    referrals_needed: int = 3
    min_ratio_to_gas: int = 3
    off: bool = False

    def policy(self) -> FeePolicy:
        """Правила для расчётов: неизменяемые и на один момент времени."""
        return FeePolicy(
            wallet="" if self.off else self.wallet,
            deposit_bps=self.deposit_bps,
            profit_bps=self.profit_bps,
            referrals_needed=self.referrals_needed,
            min_ratio_to_gas=self.min_ratio_to_gas,
        )

    @property
    def enabled(self) -> bool:
        return self.policy().enabled

    def to_state(self) -> str:
        """Строка для базы: «off;wallet;deposit;profit;refs»."""
        return ";".join([
            "1" if self.off else "0", self.wallet,
            str(self.deposit_bps), str(self.profit_bps), str(self.referrals_needed),
        ])

    def apply_state(self, raw: str) -> None:
        """Накладывает сохранённое решение поверх значений из .env."""
        parts = (raw or "").split(";")
        if len(parts) != 5:
            return          # мусор в базе не должен ронять бота и трогать .env
        off, wallet, deposit, profit, referrals = parts
        self.off = off == "1"
        self.wallet = wallet
        for field_name, value in (("deposit_bps", deposit), ("profit_bps", profit),
                                  ("referrals_needed", referrals)):
            if value.lstrip("-").isdigit():
                setattr(self, field_name, int(value))


def _percent(raw: str) -> int:
    """Проценты от человека в базисные пункты: «2.5» → 250."""
    try:
        value = Decimal(raw.replace(",", ".").rstrip("%"))
    except Exception as exc:  # noqa: BLE001 - текст от пользователя
        raise ValueError("нужно число, например 2 или 2.5") from exc
    bps = int(value * 100)
    if bps < 0 or bps > MAX_FEE_BPS:
        raise ValueError(f"допустимо от 0 до {MAX_FEE_BPS / 100:g}%")
    return bps


def apply_fee_change(fees: FeeSettings, args: str, *, is_address=None) -> tuple[bool, str]:  # noqa: ANN001
    """Выполняет команду управления комиссиями. Возвращает (менялось, ответ).

    Разбор отделён от Telegram: правила про деньги должны читаться и проверяться
    без бота. `is_address` передаётся снаружи, чтобы модуль остался без зависимостей.
    """
    parts = (args or "").strip().split()
    action = parts[0].lower() if parts else ""
    value = parts[1] if len(parts) > 1 else ""

    if action in {"on", "вкл", "включить"}:
        if not fees.wallet:
            return False, ("Сначала укажите кошелёк сбора:\n"
                           "<code>/fees wallet 0xВашАдрес</code>")
        fees.off = False
        return True, (f"✅ Комиссии включены: пополнение {fees.deposit_bps / 100:g}% · "
                      f"прибыль {fees.profit_bps / 100:g}%\n"
                      f"Уходят на <code>{fees.wallet}</code>")

    if action in {"off", "выкл", "выключить"}:
        if fees.off:
            return False, "Комиссии и так выключены."
        fees.off = True
        return True, ("🚫 Комиссии выключены. Кошелёк сохранён — включить обратно: "
                      "<code>/fees on</code>")

    if action in {"wallet", "кошелёк"}:
        if is_address is not None and not is_address(value):
            return False, ("Нужен адрес кошелька: <code>/fees wallet 0x…</code>\n"
                           "Это ваш личный адрес, а не кошелёк пользователя бота.")
        fees.wallet = value
        fees.off = False
        return True, (f"✅ Кошелёк сбора: <code>{value}</code>\n"
                      "Комиссии включены.")

    if action in {"deposit", "пополнение", "profit", "прибыль", "refs", "друзья"}:
        try:
            if action in {"refs", "друзья"}:
                if not value.isdigit():
                    raise ValueError("нужно целое число друзей")
                fees.referrals_needed = int(value)
                return True, (f"✅ Пополнения без комиссии после "
                              f"{fees.referrals_needed} приглашённых друзей")
            bps = _percent(value)
        except ValueError as exc:
            return False, f"❌ {exc}"
        if action in {"deposit", "пополнение"}:
            fees.deposit_bps = bps
            return True, f"✅ Комиссия за пополнение: {bps / 100:g}%"
        fees.profit_bps = bps
        return True, f"✅ Комиссия с прибыли: {bps / 100:g}%"

    return False, ""      # команда не распознана — показываем экран состояния


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


MAX_BAR = 10        # длиннее полоска перестаёт читаться и начинает мешать


def referral_progress(status: FeeStatus) -> str:
    """Одна строка: сколько друзей нужно и сколько осталось.

    Условие акции бесполезно, если его видно только в /ref: комиссию человек
    замечает в момент пополнения, и именно там должно быть написано, сколько ещё
    приглашений отделяет его от бесплатных пополнений.
    """
    if status.needed <= 0:
        return ""
    if status.free_deposit:
        if status.referrals >= status.needed:
            return (f"✅ Пополнения без комиссии — приглашено "
                    f"{status.referrals} из {status.needed}")
        return ""           # освобождён по другой причине — считать друзей незачем

    done = max(0, min(status.referrals, status.needed))
    bar = ("▰" * done + "▱" * (status.needed - done)) if status.needed <= MAX_BAR else ""
    left = status.left_to_free
    return (f"👥 Друзья: {done} из {status.needed}{' ' + bar if bar else ''} · "
            f"осталось {left} — и пополнения станут без комиссии навсегда")


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
