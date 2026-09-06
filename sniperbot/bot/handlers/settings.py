"""Настройки торговли и фильтров безопасности."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import MenuCB, SetCB, cancel_kb, filters_menu, main_menu, settings_menu
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.utils.fmt import esc, parse_decimal

log = logging.getLogger(__name__)

router = Router(name="settings")

TOGGLES = {
    "auto_snipe": "Автоснайп",
    "auto_sell": "Автопродажа",
    "honeypot_check": "Проверка honeypot",
    "require_simulation": "Требовать симуляцию",
    "require_renounced": "Только renounced-токены",
    "approve_max": "Бесконечный approve",
}


@dataclass(slots=True)
class FieldSpec:
    title: str
    kind: str            # decimal | int | pct | mult
    minimum: Decimal
    maximum: Decimal
    hint: str = ""
    group: str = "settings"


FIELDS: dict[str, FieldSpec] = {
    "buy_amount": FieldSpec("Сумма покупки", "decimal", Decimal("0.0001"), Decimal("1000"),
                            "Сколько нативной монеты тратить на одну покупку, например 0.05"),
    "slippage_bps": FieldSpec("Проскальзывание", "pct", Decimal("0.1"), Decimal("99"),
                              "В процентах. Для новых пар обычно 15–30"),
    "gas_multiplier_bps": FieldSpec("Множитель газа", "mult", Decimal("1"), Decimal("5"),
                                    "Во сколько раз поднимать цену газа, например 1.2"),
    "take_profit_pct": FieldSpec("Тейк-профит", "int", Decimal(0), Decimal("100000"),
                                 "Рост в процентах для фиксации прибыли. 0 — выключить"),
    "stop_loss_pct": FieldSpec("Стоп-лосс", "int", Decimal(0), Decimal("99"),
                               "Падение в процентах для выхода. 0 — выключить"),
    "trailing_stop_pct": FieldSpec("Трейлинг-стоп", "int", Decimal(0), Decimal("99"),
                                   "Откат от максимума в процентах. 0 — выключить"),
    "sell_percent": FieldSpec("Доля продажи по TP", "int", Decimal(1), Decimal(100),
                              "Сколько процентов позиции продавать по тейк-профиту"),
    "min_liquidity": FieldSpec("Мин. ликвидность", "decimal", Decimal(0), Decimal("100000"),
                               "Минимум нативной монеты в пуле", group="filters"),
    "max_liquidity": FieldSpec("Макс. ликвидность", "decimal", Decimal(0), Decimal("1000000"),
                               "0 — без ограничения", group="filters"),
    "max_buy_tax_bps": FieldSpec("Макс. налог покупки", "pct", Decimal(0), Decimal(100),
                                 "В процентах", group="filters"),
    "max_sell_tax_bps": FieldSpec("Макс. налог продажи", "pct", Decimal(0), Decimal(100),
                                  "В процентах", group="filters"),
    "min_lp_burned_pct": FieldSpec("Мин. сожжённый LP", "int", Decimal(0), Decimal(100),
                                   "Доля LP в burn-адресах, 0 — не проверять", group="filters"),
    "max_positions": FieldSpec("Макс. позиций", "int", Decimal(1), Decimal(100),
                               "Сколько позиций автоснайп держит одновременно", group="filters"),
    "max_snipes_per_hour": FieldSpec("Снайпов в час", "int", Decimal(1), Decimal(200),
                                     "Ограничение частоты автопокупок", group="filters"),
}


class SettingsStates(StatesGroup):
    value = State()


@router.message(Command("settings"))
async def cmd_settings(message: Message, cfg: ChainSettings, chain: ChainConfig) -> None:
    await reply(message, _header(chain), settings_menu(cfg, chain.native_symbol))


@router.callback_query(MenuCB.filter(F.section == "settings"))
async def cb_settings(callback: CallbackQuery, cfg: ChainSettings, chain: ChainConfig, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, _header(chain), settings_menu(cfg, chain.native_symbol))
    await callback.answer()


@router.callback_query(MenuCB.filter(F.section == "filters"))
async def cb_filters(callback: CallbackQuery, cfg: ChainSettings, chain: ChainConfig, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(
        callback,
        "🛡 <b>Фильтры безопасности</b>\n\n"
        "Применяются и к автоснайпу, и к ручной покупке через карточку токена.",
        filters_menu(cfg, chain.native_symbol),
    )
    await callback.answer()


@router.message(Command("snipe"))
async def cmd_snipe(message: Message, user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    new_value = await _toggle(user.id, chain.key, "auto_snipe")
    cfg.auto_snipe = new_value
    status = "включён ✅" if new_value else "выключен ⛔️"
    await reply(
        message,
        f"🎯 Автоснайп в сети {esc(chain.name)} {status}\n\n"
        f"Сумма покупки: {cfg.buy_amount} {chain.native_symbol}\n"
        f"Мин. ликвидность: {cfg.min_liquidity} {chain.native_symbol}\n"
        f"Макс. налоги: {cfg.max_buy_tax_bps / 100:g}% / {cfg.max_sell_tax_bps / 100:g}%",
        main_menu(chain.name, new_value),
    )


@router.callback_query(SetCB.filter(F.action == "toggle"))
async def cb_toggle(
    callback: CallbackQuery, callback_data: SetCB, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig,
) -> None:
    field = callback_data.field
    if field not in TOGGLES:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    value = await _toggle(user.id, chain.key, field)
    setattr(cfg, field, value)
    await callback.answer(f"{TOGGLES[field]}: {'вкл' if value else 'выкл'}")
    await _rerender(callback, ctx, user, cfg, chain)


async def _rerender(
    callback: CallbackQuery, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig
) -> None:
    """Перерисовывает то же меню, из которого нажали кнопку."""
    from sniperbot.bot.views import render_main  # локальный импорт: избегаем цикла

    text = (callback.message.text or "") if callback.message else ""
    if text.startswith("🛡"):
        await safe_edit(callback, "🛡 <b>Фильтры безопасности</b>", filters_menu(cfg, chain.native_symbol))
    elif text.startswith("⚙️"):
        await safe_edit(callback, _header(chain), settings_menu(cfg, chain.native_symbol))
    else:
        async with session_scope() as session:
            count = await repo.count_open_positions(session, user.id, chain.key)
        await safe_edit(
            callback,
            await render_main(ctx, user, cfg, chain, count),
            main_menu(chain.name, cfg.auto_snipe),
        )


@router.callback_query(SetCB.filter(F.action == "edit"))
async def cb_edit(callback: CallbackQuery, callback_data: SetCB, state: FSMContext, chain: ChainConfig) -> None:
    spec = FIELDS.get(callback_data.field)
    if spec is None:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await state.set_state(SettingsStates.value)
    await state.update_data(field=callback_data.field)
    unit = {"pct": "%", "mult": "×", "decimal": chain.native_symbol}.get(spec.kind, "")
    await safe_edit(
        callback,
        f"✏️ <b>{esc(spec.title)}</b>\n\n{esc(spec.hint)}\n\n"
        f"Допустимо: {spec.minimum}–{spec.maximum} {unit}\nПришлите новое значение:",
        cancel_kb(spec.group),
    )
    await callback.answer()


@router.message(SettingsStates.value)
async def on_value(
    message: Message, state: FSMContext, user: User, cfg: ChainSettings, chain: ChainConfig
) -> None:
    data = await state.get_data()
    field = data.get("field", "")
    spec = FIELDS.get(field)
    if spec is None:
        await state.clear()
        return

    raw = parse_decimal(message.text or "")
    if raw is None:
        await reply(message, "❌ Нужно число. Попробуйте ещё раз.", cancel_kb(spec.group))
        return
    if raw < spec.minimum or raw > spec.maximum:
        await reply(
            message,
            f"❌ Значение должно быть от {spec.minimum} до {spec.maximum}.",
            cancel_kb(spec.group),
        )
        return

    stored_value = _to_stored(spec, raw)
    async with session_scope() as session:
        settings_row = await repo.get_settings(session, user.id, chain.key)
        setattr(settings_row, field, stored_value)
    setattr(cfg, field, stored_value)
    await state.clear()

    if spec.group == "filters":
        await reply(message, f"✅ {spec.title} обновлено.", filters_menu(cfg, chain.native_symbol))
    else:
        await reply(message, f"✅ {spec.title} обновлено.", settings_menu(cfg, chain.native_symbol))


# ---------------------------------------------------------------- внутренности
def _to_stored(spec: FieldSpec, value: Decimal):
    if spec.kind == "pct":
        return int(value * 100)
    if spec.kind == "mult":
        return int(value * 10_000)
    if spec.kind == "int":
        return int(value)
    return value


async def _toggle(user_id: int, chain_key: str, field: str) -> bool:
    async with session_scope() as session:
        settings_row = await repo.get_settings(session, user_id, chain_key)
        value = not bool(getattr(settings_row, field))
        setattr(settings_row, field, value)
    return value


def _header(chain: ChainConfig) -> str:
    return (
        f"⚙️ <b>Настройки</b> — {esc(chain.name)}\n\n"
        "Настройки хранятся отдельно для каждой сети.\n"
        "Нажмите на пункт, чтобы изменить значение."
    )
