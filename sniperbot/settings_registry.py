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
        if self.kind == "ladder":
            return format_ladder(str(value)) if value else "выключена"
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

        if self.kind == "ladder":
            return parse_ladder(text)

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
    Setting("ladder", "tp_ladder", "chain", "ladder", "Лестница фиксаций",
            "Ступени тейк-профита «рост:доля», например 100:50,300:30 — "
            "продать половину на +100% и ещё треть на +300%. Пусто — обычный TP",
            "exits"),
    Setting("breakeven", "breakeven_pct", "chain", "int", "Стоп в безубыток",
            "После роста на N% стоп-лосс переносится в точку входа. 0 — выключено",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(1000)),
    Setting("rugguard", "rug_guard_pct", "chain", "int", "Защита от слива ликвидности",
            "Выйти, если ликвидность пула упала на N% от максимума. 0 — выключено",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(99)),
    Setting("deadtime", "dead_timeout_min", "chain", "int", "Выход из мёртвой позиции",
            "Через сколько минут закрыть позицию, если она так и не выросла. 0 — выключено",
            "exits", unit=" мин", minimum=Decimal(0), maximum=Decimal(1440)),
    Setting("deadpct", "dead_min_pct", "chain", "int", "Порог «не мёртвая»",
            "Какой рост считается признаком жизни для таймера выше",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(1000)),
    Setting("exitgas", "exit_gas_boost_bps", "chain", "mult", "Газ на выходе",
            "Множитель газа при продаже: выходить важнее, чем экономить",
            "exits", minimum=Decimal(1), maximum=Decimal(5)),
    Setting("exitslip", "exit_slippage_bps", "chain", "pct", "Проскальзывание на выходе",
            "Отдельное проскальзывание для продажи — обычно выше входного",
            "exits", minimum=Decimal("0.1"), maximum=Decimal(99)),
    Setting("preapprove", "pre_approve", "chain", "bool", "Approve сразу после покупки",
            "Разрешение роутеру выдаётся заранее, чтобы продажа не ждала лишнюю транзакцию",
            "exits"),

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
    Setting("ownershare", "max_owner_share_pct", "chain", "int", "Макс. доля у владельца",
            "Сколько процентов предложения может держать владелец. 0 — не проверять",
            "filters", unit="%", minimum=Decimal(0), maximum=Decimal(100)),
    Setting("poolshare", "min_pool_share_pct", "chain", "int", "Мин. доля в пуле",
            "Сколько процентов предложения должно лежать в пуле. 0 — не проверять",
            "filters", unit="%", minimum=Decimal(0), maximum=Decimal(100)),
    Setting("nomint", "block_mintable", "chain", "bool", "Запрет чеканки",
            "Не покупать токены, где владелец может допечатать себе токенов", "filters"),
    Setting("noblacklist", "block_blacklist_fn", "chain", "bool", "Запрет чёрных списков",
            "Не покупать токены, где вам могут запретить продавать", "filters"),
    Setting("nopause", "block_pausable", "chain", "bool", "Запрет остановки торгов",
            "Не покупать токены с функцией паузы (часто это обычный запуск торгов)", "filters"),
    Setting("noproxy", "block_proxy", "chain", "bool", "Запрет прокси",
            "Не покупать обновляемые контракты: их код могут подменить после вашей покупки",
            "filters"),
    Setting("nobadcreators", "avoid_bad_creators", "chain", "bool", "Помнить плохих создателей",
            "Не покупать токены владельцев, на которых вы уже теряли деньги", "filters"),
    Setting("minedge", "min_edge_pct", "chain", "int", "Мин. запас прибыли",
            "Минимальная прибыль после налогов, комиссий DEX и газа. 0 — не проверять",
            "filters", unit="%", minimum=Decimal(0), maximum=Decimal(1000)),
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


def parse_ladder(text: str) -> str:
    """Разбирает «100:50,300:30» в нормализованную строку ступеней.

    Пустая строка выключает лестницу. Суммарная доля не может превышать 100%.
    """
    text = (text or "").strip().lower()
    if text in {"", "off", "выкл", "нет", "0"}:
        return ""
    steps: list[tuple[int, int]] = []
    total = 0
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError("формат: рост:доля, например 100:50,300:30")
        growth_raw, share_raw = chunk.split(":", 1)
        growth = parse_decimal(growth_raw)
        share = parse_decimal(share_raw)
        if growth is None or share is None:
            raise ValueError("формат: рост:доля, например 100:50,300:30")
        if growth <= 0 or share <= 0 or share > 100:
            raise ValueError("рост > 0, доля от 1 до 100")
        total += int(share)
        if total > 100:
            raise ValueError("сумма долей больше 100%")
        steps.append((int(growth), int(share)))
    if not steps:
        return ""
    steps.sort()
    return ",".join(f"{growth}:{share}" for growth, share in steps)


def ladder_steps(value: str | None) -> list[tuple[int, int]]:
    """Ступени лестницы как список (рост %, доля %)."""
    if not value:
        return []
    steps = []
    for chunk in str(value).split(","):
        if ":" not in chunk:
            continue
        growth, share = chunk.split(":", 1)
        try:
            steps.append((int(growth), int(share)))
        except ValueError:
            continue
    return sorted(steps)


def format_ladder(value: str) -> str:
    steps = ladder_steps(value)
    if not steps:
        return "выключена"
    return " · ".join(f"+{growth}% → {share}%" for growth, share in steps)


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
