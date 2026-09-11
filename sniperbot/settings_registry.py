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
    "momentum": "🚀 Перехват разгона",
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
    # Порог роста от цены входа: думать иксами удобнее, чем процентами, поэтому
    # такие настройки принимают и «300», и «4x», и показывают то и другое.
    growth: bool = False

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
        if self.kind == "tp":
            if cfg.tp_ladder:
                return format_ladder(str(cfg.tp_ladder))
            if not cfg.take_profit_pct:
                return "выключен"
            share = cfg.sell_percent or 100
            return (f"×{step_multiplier(int(cfg.take_profit_pct))} (+{cfg.take_profit_pct}%), "
                    + (f"продать {share}%" if share < 100 else "продать всё"))
        if self.kind == "hours":
            return format_hours(str(value))
        if self.growth and int(value) > 0:
            return f"+{value}{self.unit} (×{step_multiplier(int(value))})"
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

        if self.kind == "tp":
            # Одна ступень или несколько — решает сама запись, а не вторая настройка.
            if any(mark in text for mark in "[,;:"):
                return parse_ladder(text)
            growth = parse_growth(text)
            if growth is None:
                raise ValueError(f"нужен рост («300», «4x») или ступени {LADDER_EXAMPLE}")
            return int(growth)

        if self.kind == "hours":
            return parse_hours(text)

        if self.growth:
            number = parse_growth(text)
        else:
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

    def targets(self, value) -> dict:
        """Какие поля модели меняет это значение.

        Обычно одно, но у тейка их два: одна ступень живёт в take_profit_pct,
        несколько — в tp_ladder. Держать оба заполненными нельзя, иначе одно
        молча отменяет другое, и человек узнаёт об этом по несработавшей
        фиксации. Поэтому запись всегда чистит вторую форму.
        """
        if self.kind == "tp":
            ladder = isinstance(value, str)
            return {"tp_ladder": value if ladder else "",
                    "take_profit_pct": 0 if ladder else int(value)}
        return {self.field: value}

    def write(self, value, cfg, user=None) -> None:
        target = user if self.scope == "user" else cfg
        for field_name, item in self.targets(value).items():
            setattr(target, field_name, item)


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
    Setting("tp", "tp_ladder", "chain", "tp", "Тейк-профит",
            "Одна ступень — «4x» или «300» (доля продажи в sellpct). "
            "Несколько — [[1.5, 40], [3, 30], [10, 30]]: 40% позиции на ×1.5, "
            "30% на ×3, 30% на ×10. 0 — выключить",
            "exits"),
    Setting("sl", "stop_loss_pct", "chain", "int", "Стоп-лосс",
            "Падение в процентах для выхода. 0 — выключить",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(99)),
    Setting("trail", "trailing_stop_pct", "chain", "int", "Трейлинг-стоп",
            "Откат от максимума в процентах. 0 — выключить",
            "exits", unit="%", minimum=Decimal(0), maximum=Decimal(99)),
    Setting("sellpct", "sell_percent", "chain", "int", "Доля продажи по TP",
            "Какую часть позиции продать на тейк-профите. Меньше 100 — фиксация "
            "частями: остальное остаётся в позиции и едет дальше",
            "exits", unit="%", minimum=Decimal(1), maximum=Decimal(100)),
    Setting("autosell", "auto_sell", "chain", "bool", "Автопродажа",
            "Закрывать позиции по правилам без участия человека", "exits"),
    Setting("secure", "secure_pct", "chain", "int", "Возврат вложенного",
            "После роста на N% продать ровно столько, чтобы вернуть потраченное — "
            "дальше сделка не может стать убыточной. Остаток едет дальше. 0 — выключено",
            "exits", growth=True, unit="%", minimum=Decimal(0), maximum=Decimal(1000)),
    Setting("breakeven", "breakeven_pct", "chain", "int", "Стоп в безубыток",
            "После роста на N% стоп-лосс переносится в точку входа. 0 — выключено",
            "exits", growth=True, unit="%", minimum=Decimal(0), maximum=Decimal(1000)),
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

    # ------------------------------------------------------------ перехват разгона
    Setting("momentum", "momentum_enabled", "chain", "bool", "Перехват разгона",
            "Покупать уже торгующиеся токены, когда в них начинается движение. "
            "Включён по умолчанию, работает вместе с автоснайпом и подчиняется "
            "тем же фильтрам безопасности", "momentum"),
    Setting("momgain", "momentum_min_gain_pct", "chain", "int", "Мин. рост за окно",
            "На сколько процентов цена должна вырасти за окно наблюдения, чтобы это считалось разгоном",
            "momentum", unit="%", minimum=Decimal(1), maximum=Decimal(500)),
    Setting("mommax", "momentum_max_gain_pct", "chain", "int", "Макс. рост за окно",
            "Выше этого роста вход считается покупкой на вершине. 0 — без ограничения",
            "momentum", unit="%", minimum=Decimal(0), maximum=Decimal(5000)),
    Setting("momtrades", "momentum_min_trades", "chain", "int", "Мин. сделок за окно",
            "Меньше этого числа сделок — движение делает один-два кошелька, а не рынок",
            "momentum", minimum=Decimal(1), maximum=Decimal(1000)),
    Setting("mombuys", "momentum_min_buy_ratio_pct", "chain", "int", "Мин. доля покупок",
            "Сколько процентов сделок должны быть покупками. Ниже 50% из токена выходят",
            "momentum", unit="%", minimum=Decimal(1), maximum=Decimal(100)),
    Setting("momvol", "momentum_min_volume", "chain", "decimal", "Мин. оборот за окно",
            "Минимальный оборот пула в нативной монете за окно наблюдения",
            "momentum", minimum=Decimal(0), maximum=Decimal(10_000)),
    Setting("momage", "momentum_max_age_hours", "chain", "int", "Глубина наблюдения",
            "Сколько часов держать найденные пулы в списке наблюдения",
            "momentum", unit=" ч", minimum=Decimal(1), maximum=Decimal(720)),

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
    Setting("hours", "trade_hours", "chain", "hours", "Часы торговли",
            "Покупать только в эти часы UTC: «00-22», «14,16,21». Пусто — круглосуточно. "
            "Лучшие часы по вашим данным показывает /stats",
            "risk"),
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
    Setting("tz", "tz_offset", "user", "int", "Часовой пояс",
            "Сдвиг от UTC в часах: Москва 3, Берлин 2, Нью-Йорк −5. "
            "Нужен, чтобы отчёты показывали часы по-вашему, а не по Гринвичу",
            "ux", unit=" ч от UTC", minimum=Decimal(-12), maximum=Decimal(14)),
    Setting("restart", "notify_restart", "user", "bool", "Уведомления о перезапуске",
            "Сообщать, когда бот перезапустился и обновился ли при этом код", "ux"),
)

BY_NAME = {setting.name: setting for setting in SETTINGS}
# Лестница переехала в саму настройку тейка. Старое имя оставлено рабочим:
# оно есть в наборах, в подсказках прошлых версий и в чужих записках.
BY_NAME["ladder"] = BY_NAME["tp"]
@dataclass(frozen=True, slots=True)
class Preset:
    """Согласованный набор настроек под одну манеру торговли.

    Отдельные настройки легко переставить так, что они спорят друг с другом:
    стоп −60% рядом с трейлингом 15% просто выкидывает из каждой сделки. Пресет
    задаёт значения, которые проверены как связка.
    """

    name: str
    title: str
    summary: str
    values: dict[str, str]


PRESETS: tuple[Preset, ...] = (
    Preset(
        "careful", "🛡 Осторожный",
        "Мало сделок, жёсткие фильтры, ранняя фиксация. Для тех, кому важнее "
        "не терять, чем поймать иксы.",
        {
            "slippage": "20", "gasmode": "fast",
            "tp": "[[1.6, 40], [3, 30]]", "sl": "35", "trail": "40", "secure": "35",
            "breakeven": "40", "rugguard": "40", "deadtime": "45", "deadpct": "15",
            "exitgas": "2", "exitslip": "35",
            "minliq": "3", "buytax": "8", "selltax": "8", "ownershare": "10",
            "nomint": "on", "noblacklist": "on", "noproxy": "on", "minedge": "25",
            "maxpos": "3", "perhour": "5", "cooldown": "120", "maxloss": "4",
            "momentum": "on", "momgain": "8", "mommax": "60", "momtrades": "10",
            "mombuys": "65", "momvol": "0.5", "momage": "48",
        },
    ),
    Preset(
        "momentum", "🚀 Перехват разгона",
        "Ставка не на листинги, а на токены, которые уже растут. Пороги входа "
        "мягче, зато покупка идёт по факту движения.",
        {
            "slippage": "25", "gasmode": "fast",
            "tp": "[[1.5, 40], [2.5, 30]]", "sl": "30", "trail": "35", "secure": "40",
            "breakeven": "30", "rugguard": "40", "deadtime": "30", "deadpct": "10",
            "exitgas": "2", "exitslip": "35",
            "minliq": "2", "buytax": "10", "selltax": "10", "ownershare": "15",
            "nomint": "on", "noproxy": "on", "minedge": "15",
            "maxpos": "4", "perhour": "8", "cooldown": "60", "maxloss": "5",
            "momentum": "on", "momgain": "6", "mommax": "70", "momtrades": "6",
            "mombuys": "60", "momvol": "0.3", "momage": "72",
        },
    ),
    Preset(
        "aggressive", "⚡️ Агрессивный снайп",
        "Много сделок, широкое проскальзывание, поздняя фиксация. Убыточных "
        "сделок будет больше — расчёт на редкие крупные иксы.",
        {
            "slippage": "30", "gasmode": "turbo",
            "tp": "[[2, 50], [4, 25]]", "sl": "45", "trail": "45", "secure": "60",
            "breakeven": "50", "rugguard": "50", "deadtime": "60", "deadpct": "20",
            "exitgas": "2.5", "exitslip": "40",
            "minliq": "1", "buytax": "12", "selltax": "12", "ownershare": "20",
            "nomint": "on", "noproxy": "off", "minedge": "0",
            "maxpos": "6", "perhour": "15", "cooldown": "0", "maxloss": "6",
            "momentum": "on", "momgain": "5", "mommax": "90", "momtrades": "5",
            "mombuys": "55", "momvol": "0.2", "momage": "72",
        },
    ),
)

PRESETS_BY_NAME = {preset.name: preset for preset in PRESETS}


def preset_changes(preset: Preset, cfg) -> list[tuple[Setting, object, str]]:
    """Что пресет поменяет: (настройка, новое значение, как показать).

    Значения проходят обычный разбор `/set`, поэтому пресет не может записать
    то, что вручную записать нельзя.
    """
    changes = []
    for name, raw in preset.values.items():
        setting = BY_NAME.get(name)
        if setting is None or setting.scope != "chain":
            continue
        value = setting.parse(raw)
        if setting.read(cfg) == value:
            continue
        changes.append((setting, value, raw))
    return changes

GAS_MODE_MULTIPLIERS = {"normal": 11_000, "fast": 15_000, "turbo": 25_000}


def parse_hours(text: str) -> str:
    """Разбирает «00-22», «14,16,21», «0-3,20-22» в нормализованный список часов.

    Пустая строка означает круглосуточную торговлю. Часы всегда UTC: у сервера,
    у отчётов и у этой настройки должно быть одно время, иначе «лучшие часы» из
    /stats и «торговать в эти часы» разъедутся.
    """
    text = (text or "").strip().lower()
    if text in {"", "off", "выкл", "нет", "все", "all", "24/7"}:
        return ""
    hours: set[int] = set()
    for chunk in text.replace(";", ",").replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            if not (start_raw.isdigit() and end_raw.isdigit()):
                raise ValueError("формат: 00-22 либо 14,16,21")
            start, end = int(start_raw), int(end_raw)
            if not (0 <= start <= 23 and 0 <= end <= 23):
                raise ValueError("часы от 0 до 23")
            # Интервал через полночь («22-3») — это тоже осмысленное окно.
            hours |= set(range(start, end + 1)) if start <= end else (
                set(range(start, 24)) | set(range(0, end + 1)))
        elif chunk.isdigit():
            hour = int(chunk)
            if not 0 <= hour <= 23:
                raise ValueError("часы от 0 до 23")
            hours.add(hour)
        else:
            raise ValueError("формат: 00-22 либо 14,16,21")
    if not hours:
        return ""
    if len(hours) == 24:
        return ""      # все часы = ограничения нет
    return ",".join(f"{hour:02d}" for hour in sorted(hours))


def hours_set(value: str | None) -> set[int]:
    return {int(part) for part in str(value or "").split(",") if part.strip().isdigit()}


def format_hours(value: str | None) -> str:
    """«00,01,02,14» → «00-02, 14» — так окно читается с одного взгляда."""
    hours = sorted(hours_set(value))
    if not hours:
        return "круглосуточно"
    spans, start, previous = [], hours[0], hours[0]
    for hour in hours[1:]:
        if hour == previous + 1:
            previous = hour
            continue
        spans.append((start, previous))
        start = previous = hour
    spans.append((start, previous))
    return ", ".join(f"{a:02d}" if a == b else f"{a:02d}-{b:02d}" for a, b in spans) + " UTC"


def trading_allowed(value: str | None, now=None) -> bool:  # noqa: ANN001 - datetime
    """Разрешена ли торговля сейчас. Пустая настройка — разрешена всегда."""
    import datetime as _dt

    hours = hours_set(value)
    if not hours:
        return True
    moment = now or _dt.datetime.now(_dt.UTC)
    return moment.hour in hours


LADDER_EXAMPLE = "[[1.5, 40], [3, 30], [10, 30]]"
MIN_STEP_GROWTH = 5      # ступень ниже — почти наверняка перепутанный множитель


def _ladder_pairs(text: str) -> list[tuple[str, str, bool]]:
    """Пары «ступень, доля» из обеих записей вместе с признаком множителя.

    Множителями думать удобнее: ×3 понятнее, чем +200%. Скобочная запись целиком
    про множители, запись через двоеточие исторически про рост в процентах —
    каждая читается однозначно, и гадать по величине числа не приходится.
    """
    if "[" in text or "]" in text:
        flat = [chunk.strip() for chunk in
                text.replace("[", " ").replace("]", " ").replace(";", ",").split(",")
                if chunk.strip()]
        if not flat or len(flat) % 2:
            raise ValueError(f"формат: {LADDER_EXAMPLE}")
        return [(flat[i], flat[i + 1], True) for i in range(0, len(flat), 2)]

    pairs: list[tuple[str, str, bool]] = []
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"формат: {LADDER_EXAMPLE} или 100:50,300:30")
        step, share = chunk.split(":", 1)
        pairs.append((step, share, False))
    return pairs


def _step_growth(raw: str, multiplier_by_default: bool) -> int:
    """Ступень в процентах роста: ×1.5 и +50% — одно и то же."""
    value = raw.strip().lower().replace("×", "x").replace("+", "")
    as_multiplier = value.startswith("x") or value.endswith("x")
    as_percent = value.endswith("%")
    number = parse_decimal(value.strip("x%").strip())
    if number is None:
        raise ValueError(f"не понял ступень «{raw.strip()}». Формат: {LADDER_EXAMPLE}")

    if as_multiplier or (multiplier_by_default and not as_percent):
        if number <= 1:
            raise ValueError(f"множитель должен быть больше 1: ×1.5 — это +50%, а ×{number:g} — убыток")
        return int(round((number - 1) * 100))
    if number < MIN_STEP_GROWTH:
        raise ValueError(
            f"ступень +{number:g}% слишком близко ко входу. Если имелся в виду "
            f"множитель ×{number:g}, напишите <code>{number:g}x</code> или {LADDER_EXAMPLE}"
        )
    return int(number)


def parse_growth(text: str) -> Decimal | None:
    """Порог роста в процентах. «4x» и «×4» — это +300%, «300» — тоже.

    Множитель распознаётся только по явному x: голое число остаётся процентами,
    иначе «/set tp 4» тихо превратилось бы из +4% в ×4.
    """
    value = str(text).strip().lower().replace("×", "x").replace("+", "")
    if not (value.startswith("x") or value.endswith("x")):
        return parse_decimal(value)
    number = parse_decimal(value.strip("x").strip())
    if number is None:
        return None
    if number <= 1:
        raise ValueError(f"множитель должен быть больше 1: ×2 — это +100%, а ×{number:g} — убыток")
    return (number - 1) * 100


def parse_ladder(text: str) -> str:
    """Разбирает лестницу в нормализованную строку ступеней «рост:доля».

    Принимает и множители — [[1.5, 40], [3, 30]] или 1.5x:40 — и проценты роста
    (100:50). Внутри всё живёт в процентах: так ступень одинаково понимается и в
    настройках, и в уже открытых позициях. Пустая строка выключает лестницу,
    суммарная доля не может превышать 100%.
    """
    text = (text or "").strip().lower()
    if text in {"", "off", "выкл", "нет", "0", "[]"}:
        return ""

    steps: list[tuple[int, int]] = []
    total = 0
    for step_raw, share_raw, multiplier in _ladder_pairs(text):
        growth = _step_growth(step_raw, multiplier)
        share = parse_decimal(share_raw.strip().rstrip("%"))
        if share is None:
            raise ValueError(f"не понял долю «{share_raw.strip()}». Формат: {LADDER_EXAMPLE}")
        if share <= 0 or share > 100:
            raise ValueError("доля ступени — от 1 до 100%")
        total += int(share)
        if total > 100:
            raise ValueError("сумма долей больше 100%: продать больше позиции нельзя")
        steps.append((growth, int(share)))
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


def step_multiplier(growth: int) -> str:
    """Ступень как множитель цены: +200% это ×3.

    Хвостовые нули убираются вручную: normalize() превращает 10 в 1E+1.
    """
    text = f"{Decimal(100 + growth) / 100:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_ladder(value: str) -> str:
    steps = ladder_steps(value)
    if not steps:
        return "выключена"
    return " · ".join(f"×{step_multiplier(growth)} → {share}%" for growth, share in steps)


def ladder_note(value: str, secure_pct: int = 0) -> str:
    """Та же лестница в процентах роста — чтобы не гадать, что понял бот.

    Если включён возврат вложенного, он продаёт из той же позиции и закрывает
    ступени, которые уже перекрыл по объёму. Об этом лучше сказать сразу, чем
    оставить человека выяснять, почему ступень ×3 не сработала.
    """
    steps = ladder_steps(value)
    if not steps:
        return ""
    growth = " · ".join(f"+{step}% → {share}%" for step, share in steps)
    total = sum(share for _, share in steps)
    lead = "Сами ступени" if secure_pct > 0 else "Ступени"
    tail = (f"{lead} фиксируют {total}% позиции, остальные {100 - total}% едут дальше."
            if total < 100 else f"{lead} продают позицию целиком — бегунка не останется.")
    note = f"\nТо есть: {growth}\n{tail}"

    if secure_pct > 0:
        sold = Decimal(10_000) / (100 + secure_pct)      # доля, возвращающая вложенное
        covered, running = [], Decimal(0)
        for step, share in steps:
            running += share
            if running > sold:
                break
            covered.append(f"×{step_multiplier(step)}")
        left = max(Decimal(0), 100 - sold)
        note += (f"\n\n🛟 Возврат вложенного на +{secure_pct}% продаст около {sold:.0f}% позиции"
                 + (f" и закроет ступени {', '.join(covered)}" if covered else "")
                 + f" — дальше поедет примерно {left:.0f}%.")
        if covered:
            note += ("\nЧтобы ступени срабатывали сами, поднимите порог возврата "
                     "(<code>/set secure 100</code>) или выключите его "
                     "(<code>/set secure 0</code>).")
    return note


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


class VariantOverlay:
    """Настройки варианта B поверх основных: читается как обычный объект настроек."""

    __slots__ = ("_base", "_overrides")

    def __init__(self, base, overrides: dict) -> None:
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name: str):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)


def parse_variant(raw: str | None) -> dict:
    """Разбирает JSON варианта из БД, отбрасывая мусор."""
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}


def variant_overlay(cfg, variant: dict):
    """Возвращает cfg с применёнными настройками варианта (или сам cfg)."""
    overrides = {}
    for name, raw in variant.items():
        setting = find(name)
        if setting is None or setting.scope != "chain":
            continue
        try:
            overrides.update(setting.targets(setting.parse(str(raw))))
        except ValueError:
            continue
    return VariantOverlay(cfg, overrides) if overrides else cfg


def describe_variant(variant: dict) -> str:
    parts = []
    for name, raw in variant.items():
        setting = find(name)
        title = setting.title if setting else name
        parts.append(f"{title} = {raw}")
    return "; ".join(parts) if parts else "пусто"


def render_compact(cfg, user=None, native: str = "") -> str:
    """Все настройки одним экраном: только имена и значения."""
    lines = []
    grouped = by_group()
    for group, title in GROUPS.items():
        items = grouped.get(group, [])
        if not items:
            continue
        lines.append(f"\n<b>{title}</b>")
        for setting in items:
            lines.append(f"<code>{setting.name}</code> — {setting.display(cfg, user, native)}")
    return "\n".join(lines).strip()


def render_full(cfg, user=None, native: str = "", group: str | None = None) -> str:
    """То же, но с пояснением к каждой настройке."""
    lines = []
    grouped = by_group()
    for key, title in GROUPS.items():
        if group and key != group:
            continue
        items = grouped.get(key, [])
        if not items:
            continue
        lines.append(f"\n<b>{title}</b>")
        for setting in items:
            lines.append(
                f"<code>{setting.name}</code> · {setting.title}: "
                f"<b>{setting.display(cfg, user, native)}</b>\n    <i>{setting.hint}</i>"
            )
    return "\n".join(lines).strip()


def render_one(setting: Setting, cfg, user=None, native: str = "") -> str:
    """Карточка одной настройки: что это, сколько сейчас и что можно ставить."""
    if setting.kind == "bool":
        allowed = "on / off"
    elif setting.kind == "choice":
        allowed = " · ".join(f"<code>{choice}</code>" for choice in setting.choices)
    elif setting.kind in {"ladder", "tp"}:
        allowed = (f"множителями <code>{LADDER_EXAMPLE}</code> — ×1.5 → 40%, ×3 → 30%, ×10 → 30%\n"
                   "или ростом в процентах <code>50:40,200:30,900:30</code> — это то же самое\n"
                   "<code>off</code> — выключить")
    else:
        bounds = [str(setting.minimum) if setting.minimum is not None else "",
                  str(setting.maximum) if setting.maximum is not None else ""]
        unit = setting.unit or (native if setting.kind == "decimal" else "")
        allowed = f"{'–'.join(b for b in bounds if b)} {unit}".strip() or "любое число"

    scope = "общая для всех сетей" if setting.scope == "user" else "своя для каждой сети"
    return (
        f"⚙️ <b>{setting.title}</b>\n"
        f"<i>{setting.hint}</i>\n\n"
        f"Сейчас: <b>{setting.display(cfg, user, native)}</b>\n"
        f"Допустимо: {allowed}\n"
        f"Область: {scope}\n\n"
        f"Изменить: <code>/set {setting.name} значение</code>"
    )


def apply_value(name: str, raw: str, cfg, user=None) -> tuple[Setting, object]:
    """Разбирает и записывает значение. Бросает ValueError с текстом для пользователя."""
    setting = find(name)
    if setting is None:
        raise ValueError(f"неизвестная настройка «{name}»")
    value = setting.parse(raw)
    setting.write(value, cfg, user)
    return setting, value
