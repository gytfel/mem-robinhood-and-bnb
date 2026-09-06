"""Реестр настроек бота — единый источник правды.

Одно описание используется сразу везде: команда `/set`, экран `/config`,
кнопки настроек и валидация значений. Добавить новую настройку = добавить
запись сюда и поле в модель.

`scope` говорит, где хранится значение:

* ``chain`` — в :class:`ChainSettings`, своё для каждой сети;
* ``user``  — в :class:`User`, общее для всех сетей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sniperbot.utils.fmt import fmt_amount, parse_decimal

GROUPS = {
    "trade": "💰 Торговля",
    "exits": "🎯 Выходы",
    "filters": "🛡 Фильтры безопасности",
    "risk": "🚦 Риск-лимиты",
    "ux": "🔔 Прочее",
}


@dataclass(frozen=True, slots=True)
class Setting:
    name: str                    # короткое имя для /set
    field: str                   # поле модели
    scope: str                   # chain | user
    kind: str                    # decimal | int | pct | mult | bool | choice
    title: str
    hint: str = ""
    group: str = "trade"
    unit: str = ""
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------- значения
    def read(self, cfg, user=None):
        source = user if self.scope == "user" else cfg
        return getattr(source, self.field, None)

    def display(self, cfg, user=None, native: str = "") -> str:
        value = self.read(cfg, user)
        if value is None:
            return "—"
        if self.kind == "bool":
            return "вкл" if value else "выкл"
        if self.kind == "pct":
            return f"{Decimal(value) / 100:g}%"
        if self.kind == "mult":
            return f"×{Decimal(value) / 10_000:g}"
        if self.kind == "decimal":
            return f"{fmt_amount(value)} {self.unit or native}".strip()
        if self.kind == "choice":
            return str(value)
        return f"{value}{self.unit}"

    def parse(self, raw: str):
        """Разбор пользовательского ввода. Бросает ValueError с понятным текстом."""
        text = str(raw).strip()
        if self.kind == "bool":
            if text.lower() in {"on", "вкл", "1", "true", "да", "yes"}:
                return True
            if text.lower() in {"off", "выкл", "0", "false", "нет", "no"}:
                return False
            raise ValueError("нужно on или off")
        if self.kind == "choice":
            value = text.lower()
            if value not in self.choices:
                raise ValueError("допустимо: " + ", ".join(self.choices))
            return value

        number = parse_decimal(text)
        if number is None:
            raise ValueError("нужно число")
        if self.minimum is not None and number < self.minimum:
            raise ValueError(f"минимум {self.minimum}")
        if self.maximum is not None and number > self.maximum:
            raise ValueError(f"максимум {self.maximum}")

        if self.kind == "pct":
            return int(number * 100)
        if self.kind == "mult":
            return int(number * 10_000)
        if self.kind == "int":
            return int(number)
        return number

    def write(self, value, cfg, user=None) -> None:
        target = user if self.scope == "user" else cfg
        setattr(target, self.field, value)


SETTINGS: tuple[Setting, ...] = (
    # --------------------------------------------------------------- торговля
    Setting("buy", "buy_amount", "chain", "decimal", "Сумма покупки",
            "Сколько нативной монеты тратить на одну покупку",
            "trade", minimum=Decimal("0.0001"), maximum=Decimal(1000)),
    Setting("slippage", "slippage_bps", "chain", "pct", "Проскальзывание",
            "Допустимое отклонение цены. Для новых пар обычно 15–30%",
            "trade", minimum=Decimal("0.1"), maximum=Decimal(99)),
    Setting("gas", "gas_multiplier_bps", "chain", "mult", "Множитель газа",
            "Во сколько раз поднимать цену газа против базовой",
            "trade", minimum=Decimal(1), maximum=Decimal(5)),
    Setting("gasmode", "gas_mode", "chain", "choice", "Режим газа",
            "normal ×1.1 · fast ×1.5 · turbo ×2.5 · manual — значение gas",
            "trade", choices=("normal", "fast", "turbo", "manual")),
    Setting("gaslimit", "gas_limit", "chain", "int", "Лимит газа",
            "Верхняя граница газа на сделку",
            "trade", minimum=Decimal(100_000), maximum=Decimal(5_000_000)),
    Setting("priority", "priority_fee_gwei", "chain", "decimal", "Приоритетная комиссия",
            "Только для сетей с EIP-1559 (Robinhood Chain). В gwei",
            "trade", unit="gwei", minimum=Decimal(0), maximum=Decimal(500)),
    Setting("route", "dex_route", "chain", "choice", "Площадка",
            "auto — самый ликвидный пул, либо жёстко v2 / v3",
            "trade", choices=("auto", "v2", "v3")),
    Setting("approve", "approve_max", "chain", "bool", "Бесконечный approve",
            "Разрешить роутеру списывать токен без лимита — быстрее продажа",
            "trade"),
    Setting("autosnipe", "auto_snipe", "chain", "bool", "Автоснайп",
            "Покупать новые пулы автоматически", "trade"),

    # ----------------------------------------------------------------- выходы
    Setting("tp", "take_profit_pct", "chain", "int", "Тейк-профит",
            "Рост в процентах для фиксации прибыли. 0 — выключить",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(100_000)),
    Setting("sl", "stop_loss_pct", "chain", "int", "Стоп-лосс",
            "Падение в процентах для выхода. 0 — выключить",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(99)),
    Setting("trail", "trailing_stop_pct", "chain", "int", "Трейлинг-стоп",
            "Откат от максимума в процентах. 0 — выключить",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(99)),
    Setting("sellpct", "sell_percent", "chain", "int", "Доля продажи по TP",
            "Сколько процентов позиции продавать по тейк-профиту",
            "exits", unit="%", minimum=Decimal(1), maximum=Decimal(100)),
    Setting("autosell", "auto_sell", "chain", "bool", "Автопродажа",
            "Закрывать позиции по правилам без участия человека", "exits"),

    # ---------------------------------------------------------------- фильтры
    Setting("minliq", "min_liquidity", "chain", "decimal", "Мин. ликвидность",
            "Минимум нативной монеты в пуле",
            "filters", minimum=Decimal(0), maximum=Decimal(100_000)),
    Setting("maxliq", "max_liquidity", "chain", "decimal", "Макс. ликвидность",
            "0 — без ограничения",
            "filters", minimum=Decimal(0), maximum=Decimal(1_000_000)),
    Setting("buytax", "max_buy_tax_bps", "chain", "pct", "Макс. налог покупки",
            "Выше этого — не покупаем", "filters", minimum=Decimal(0), maximum=Decimal(100)),
    Setting("selltax", "max_sell_tax_bps", "chain", "pct", "Макс. налог продажи",
            "Выше этого — не покупаем", "filters", minimum=Decimal(0), maximum=Decimal(100)),
    Setting("honeypot", "honeypot_check", "chain", "bool", "Проверка honeypot",
            "Симулировать продажу перед покупкой", "filters"),
    Setting("sim", "require_simulation", "chain", "bool", "Требовать симуляцию",
            "Не покупать, если нода не поддерживает симуляцию", "filters"),
    Setting("renounced", "require_renounced", "chain", "bool", "Только renounced",
            "Покупать лишь токены без владельца", "filters"),
    Setting("lpburn", "min_lp_burned_pct", "chain", "int", "Мин. сожжённый LP",
            "Доля LP в burn-адресах. 0 — не проверять. В V3 не применяется",
            "filters", unit="%", minimum=Decimal(0), maximum=Decimal(100)),

    # ------------------------------------------------------------------ риск
    Setting("maxpos", "max_positions", "chain", "int", "Макс. позиций",
            "Сколько позиций автоснайп держит одновременно",
            "risk", minimum=Decimal(1), maximum=Decimal(100)),
    Setting("perhour", "max_snipes_per_hour", "chain", "int", "Снайпов в час",
            "Ограничение частоты автопокупок",
            "risk", minimum=Decimal(1), maximum=Decimal(200)),
    Setting("cooldown", "cooldown_seconds", "chain", "int", "Пауза между покупками",
            "Сколько секунд ждать после автопокупки. 0 — без паузы",
            "risk", unit=" c", minimum=Decimal(0), maximum=Decimal(3600)),
    Setting("dayloss", "daily_loss_limit", "chain", "decimal", "Дневной лимит убытка",
            "Автоснайп останавливается, если за сутки потеряно больше. 0 — выключено",
            "risk", minimum=Decimal(0), maximum=Decimal(1000)),
    Setting("maxloss", "max_consecutive_losses", "chain", "int", "Убытков подряд",
            "Стоп после N убыточных сделок подряд. Сбрасывается командой /on. 0 — выключено",
            "risk", minimum=Decimal(0), maximum=Decimal(50)),

    # ---------------------------------------------------------------- прочее
    Setting("dry", "dry_run", "user", "bool", "Тестовый режим",
            "Сделки только на бумаге, реальные деньги не тратятся", "ux"),
    Setting("notify", "notify_level", "user", "choice", "Уведомления",
            "all — всё, trades — только сделки, errors — только ошибки",
            "ux", choices=("all", "trades", "errors")),
    Setting("deposits", "notify_deposits", "user", "bool", "Уведомления о пополнении",
            "Сообщать о приходе средств на кошелёк", "ux"),
)

BY_NAME = {setting.name: setting for setting in SETTINGS}
GAS_MODE_MULTIPLIERS = {"normal": 11_000, "fast": 15_000, "turbo": 25_000}


def find(name: str) -> Setting | None:
    return BY_NAME.get(str(name).strip().lower().lstrip("/"))


def by_group() -> dict[str, list[Setting]]:
    grouped: dict[str, list[Setting]] = {key: [] for key in GROUPS}
    for setting in SETTINGS:
        grouped.setdefault(setting.group, []).append(setting)
    return grouped


def effective_gas_multiplier(cfg) -> Decimal:
    """Множитель газа с учётом режима: manual берёт значение из настройки."""
    mode = getattr(cfg, "gas_mode", "manual") or "manual"
    bps = GAS_MODE_MULTIPLIERS.get(mode, int(getattr(cfg, "gas_multiplier_bps", 12_000)))
    return Decimal(bps) / 10_000


def apply_value(name: str, raw: str, cfg, user=None) -> tuple[Setting, object]:
    """Разбирает и записывает значение. Бросает ValueError с текстом для пользователя."""
    setting = find(name)
    if setting is None:
        raise ValueError(f"неизвестная настройка «{name}»")
    value = setting.parse(raw)
    setting.write(value, cfg, user)
    return setting, value
