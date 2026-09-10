"""Настройки: экраны, кнопки, /config и /set."""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import GroupCB, MenuCB, SetCB, cancel_kb, group_menu, main_menu, settings_menu
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.pairstats import round_trip_cost
from sniperbot.settings_registry import (
    GROUPS,
    PRESETS,
    PRESETS_BY_NAME,
    SETTINGS,
    find,
    preset_changes,
    render_compact,
    render_full,
    render_one,
)
from sniperbot.utils.fmt import esc, fmt_amount

log = logging.getLogger(__name__)

router = Router(name="settings")

TYPICAL_SWAP_GAS = 200_000   # пока своих замеров нет — обычная цена свопа


class SettingsStates(StatesGroup):
    value = State()


# ------------------------------------------------------------------- экраны
@router.message(Command("settings"))
async def cmd_settings(message: Message, cfg: ChainSettings, chain: ChainConfig, user: User) -> None:
    await reply(message, _header(chain), settings_menu(cfg, chain.native_symbol, user))


@router.callback_query(MenuCB.filter(F.section == "settings"))
async def cb_settings(callback: CallbackQuery, cfg: ChainSettings, chain: ChainConfig,
                      user: User, state: FSMContext) -> None:
    await state.clear()
    await safe_edit(callback, _header(chain), settings_menu(cfg, chain.native_symbol, user))
    await callback.answer()


@router.callback_query(MenuCB.filter(F.section == "filters"))
async def cb_filters(callback: CallbackQuery, cfg: ChainSettings, chain: ChainConfig,
                     user: User, state: FSMContext) -> None:
    await state.clear()
    await _show_group(callback, "filters", cfg, chain, user)


@router.callback_query(GroupCB.filter())
async def cb_group(callback: CallbackQuery, callback_data: GroupCB, cfg: ChainSettings,
                   chain: ChainConfig, user: User, state: FSMContext) -> None:
    await state.clear()
    await _show_group(callback, callback_data.group, cfg, chain, user)


async def _show_group(callback: CallbackQuery, group: str, cfg: ChainSettings,
                      chain: ChainConfig, user: User) -> None:
    title = GROUPS.get(group, group)
    await safe_edit(
        callback,
        f"{title} — {esc(chain.name)}\n\nНажмите на пункт, чтобы изменить значение.",
        group_menu(group, cfg, chain.native_symbol, user),
    )
    await callback.answer()


# -------------------------------------------------------------- /config, /set
@router.message(Command("config"))
async def cmd_config(message: Message, command: CommandObject, cfg: ChainSettings,
                     chain: ChainConfig, user: User) -> None:
    """Показывает настройки: кратко, подробно, по группе или по одной."""
    arg = (command.args or "").strip().lower()
    native = chain.native_symbol
    header = f"⚙️ <b>Настройки</b> — {esc(chain.name)}"

    if arg == "all":                       # сырой список для копирования
        rows = "\n".join(f"{setting.name:<12} {setting.display(cfg, user, native)}"
                          for setting in SETTINGS)
        await reply(message, f"{header}\n<pre>{rows}</pre>")
        return

    if arg in GROUPS:                      # одна группа с пояснениями
        await reply(message, f"{header}\n{render_full(cfg, user, native, group=arg)}",
                    group_menu(arg, cfg, native, user))
        return

    if arg in {"full", "полностью", "все", "всё"}:
        await reply(message, f"{header}\n{render_full(cfg, user, native)}")
        return

    if arg:                                # карточка одной настройки
        setting = find(arg)
        if setting is None:
            await reply(message, f"❌ Настройки «{esc(arg)}» нет. Полный список: /config")
            return
        await reply(message, render_one(setting, cfg, user, native))
        return

    groups = " · ".join(f"<code>/config {key}</code>" for key in GROUPS)
    await reply(
        message,
        f"{header}\n{render_compact(cfg, user, native)}\n\n"
        f"Всего настроек: {len(SETTINGS)}\n"
        f"Подробно: <code>/config full</code> · по одной: <code>/config tp</code>\n"
        f"По группам: {groups}\n"
        f"Изменить: <code>/set имя значение</code>",
        settings_menu(cfg, native, user),
    )


@router.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject, ctx: BotContext,
                  cfg: ChainSettings, chain: ChainConfig, user: User) -> None:
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await reply(
            message,
            "Использование: <code>/set имя значение</code>\n"
            "Примеры:\n"
            "<code>/set buy 0.05</code> — сумма покупки\n"
            "<code>/set tp 200</code> — тейк-профит +200%\n"
            "<code>/set gasmode turbo</code> — режим газа\n"
            "<code>/set route v3</code> — торговать только через V3\n\n"
            "Полный список: /config",
        )
        return

    name, raw = parts[0], parts[1]
    setting = find(name)
    if setting is None:
        await reply(message, f"❌ Неизвестная настройка «{esc(name)}». Список: /config")
        return

    try:
        value = setting.parse(raw)
    except ValueError as exc:
        await reply(message, f"❌ {esc(setting.title)}: {esc(exc)}")
        return

    await _persist(user.id, chain.key, setting, value, cfg, user)
    text = (
        f"✅ <b>{esc(setting.title)}</b> = {esc(setting.display(cfg, user, chain.native_symbol))}"
        + ("\n<i>Настройка общая для всех сетей</i>" if setting.scope == "user"
           else f"\n<i>Только для сети {esc(chain.name)}</i>")
    )
    if setting.name == "buy":
        text += await _gas_warning(ctx, user, chain, value)
    await reply(message, text)


async def _gas_warning(ctx: BotContext, user: User, chain: ChainConfig, amount) -> str:  # noqa: ANN001
    """Предупреждение, если газ съедает вход.

    Газ — плата за транзакцию, а не процент от суммы: он одинаков для любого
    входа. Узнавать об этом в момент сделки поздно, поэтому считаем сразу.
    """
    try:
        fees = await ctx.registry.get(chain.key).gas_fees()
    except Exception as exc:  # noqa: BLE001 - без цены газа просто молчим
        log.debug("Цена газа недоступна: %s", exc)
        return ""
    price = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0)
    if not price:
        return ""

    async with session_scope() as session:
        measured = await repo.gas_by_kind(session, user.id, chain.key)
    units = (measured.get("buy", 0) + measured.get("sell", 0)) or 2 * TYPICAL_SWAP_GAS
    cost, share = round_trip_cost(units, price, Decimal(str(amount)), chain.native_decimals)
    if share < 5:
        return (f"\n\nГаз за круг «купил-продал»: ~{fmt_amount(cost, 6)} {chain.native_symbol} "
                f"— это {share:.1f}% от входа.")

    sane = cost * 20      # чтобы газ был не больше 5% входа
    return (
        f"\n\n⚠️ <b>Газ съест сделку.</b> Круг «купил-продал» стоит около "
        f"{fmt_amount(cost, 6)} {chain.native_symbol} — это <b>{share:.0f}%</b> от входа "
        f"{fmt_amount(Decimal(str(amount)))}.\n"
        "Газ не зависит от суммы: он одинаков и для 0.0002, и для целой монеты. "
        "Чем меньше вход, тем большую долю он забирает.\n"
        f"Чтобы газ был в пределах 5%, вход должен быть от "
        f"<code>/set buy {fmt_amount(sane, 4)}</code>"
    )


@router.message(Command("preset"))
async def cmd_preset(message: Message, command: CommandObject, cfg: ChainSettings,
                     chain: ChainConfig, user: User) -> None:
    """Применяет согласованный набор настроек одной командой."""
    name = (command.args or "").strip().lower()
    if not name:
        lines = ["🎛 <b>Готовые наборы настроек</b>\n"]
        for preset in PRESETS:
            lines.append(f"<b>{esc(preset.title)}</b> — <code>/preset {preset.name}</code>\n"
                         f"{esc(preset.summary)}\n")
        lines.append("Набор меняет только настройки текущей сети и не трогает сумму "
                     "покупки. После применения проверьте /config и погоняйте в /dry.")
        await reply(message, "\n".join(lines))
        return

    preset = PRESETS_BY_NAME.get(name)
    if preset is None:
        available = " · ".join(f"<code>{item.name}</code>" for item in PRESETS)
        await reply(message, f"❌ Набора «{esc(name)}» нет. Доступны: {available}")
        return

    changes = preset_changes(preset, cfg)
    for setting, value, _ in changes:
        await _persist(user.id, chain.key, setting, value, cfg, user)

    if not changes:
        await reply(message, f"🎛 <b>{esc(preset.title)}</b> уже применён — менять нечего.")
        return

    shown = [f"· {esc(setting.title)}: {esc(setting.display(cfg, user, chain.native_symbol))}"
             for setting, _, _ in changes[:12]]
    if len(changes) > 12:
        shown.append(f"· …и ещё {len(changes) - 12} — смотрите /config")
    await reply(
        message,
        f"🎛 <b>{esc(preset.title)}</b> применён для сети {esc(chain.name)}\n"
        f"{esc(preset.summary)}\n\n"
        + "\n".join(shown)
        + "\n\nСумма покупки не менялась: <code>/set buy 0.01</code>. "
          "Прежде чем включать боевой режим, проверьте набор в /dry.",
    )


# ---------------------------------------------------------------- кнопки
@router.callback_query(SetCB.filter(F.action == "toggle"))
async def cb_toggle(callback: CallbackQuery, callback_data: SetCB, ctx: BotContext,
                    user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    setting = find(callback_data.field)
    if setting is None or setting.kind != "bool":
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    value = not bool(setting.read(cfg, user))
    await _persist(user.id, chain.key, setting, value, cfg, user)
    await callback.answer(f"{setting.title}: {'вкл' if value else 'выкл'}")
    await _rerender(callback, ctx, user, cfg, chain, setting.group)


@router.callback_query(SetCB.filter(F.action == "edit"))
async def cb_edit(callback: CallbackQuery, callback_data: SetCB, state: FSMContext,
                  chain: ChainConfig) -> None:
    setting = find(callback_data.field)
    if setting is None:
        await callback.answer("Неизвестная настройка", show_alert=True)
        return
    await state.set_state(SettingsStates.value)
    await state.update_data(field=setting.name, group=setting.group)

    if setting.kind == "choice":
        allowed = "Допустимые значения: " + ", ".join(f"<code>{c}</code>" for c in setting.choices)
    else:
        bounds = []
        if setting.minimum is not None:
            bounds.append(str(setting.minimum))
        if setting.maximum is not None:
            bounds.append(str(setting.maximum))
        unit = setting.unit or (chain.native_symbol if setting.kind == "decimal" else "")
        allowed = f"Допустимо: {'–'.join(bounds)} {unit}".strip() if bounds else ""

    await safe_edit(
        callback,
        f"✏️ <b>{esc(setting.title)}</b>\n\n{esc(setting.hint)}\n\n{allowed}\n\nПришлите новое значение:",
        cancel_kb("settings"),
    )
    await callback.answer()


@router.message(SettingsStates.value)
async def on_value(message: Message, state: FSMContext, ctx: BotContext, user: User,
                   cfg: ChainSettings, chain: ChainConfig) -> None:
    data = await state.get_data()
    setting = find(data.get("field", ""))
    if setting is None:
        await state.clear()
        return
    try:
        value = setting.parse(message.text or "")
    except ValueError as exc:
        await reply(message, f"❌ {esc(exc)}. Попробуйте ещё раз.", cancel_kb("settings"))
        return

    await _persist(user.id, chain.key, setting, value, cfg, user)
    await state.clear()
    await reply(
        message,
        f"✅ <b>{esc(setting.title)}</b> = {esc(setting.display(cfg, user, chain.native_symbol))}",
        group_menu(setting.group, cfg, chain.native_symbol, user),
    )


# ------------------------------------------------------------- внутренности
async def _persist(user_id: int, chain_key: str, setting, value, cfg: ChainSettings, user: User) -> None:
    """Пишет значение в БД и в объекты, которые уже держит хендлер."""
    async with session_scope() as session:
        if setting.scope == "user":
            stored_user = await session.get(User, user_id)
            if stored_user is not None:
                setting.write(value, cfg, stored_user)
        else:
            stored_cfg = await repo.get_settings(session, user_id, chain_key)
            setting.write(value, stored_cfg, user)
    setting.write(value, cfg, user)


async def _rerender(callback: CallbackQuery, ctx: BotContext, user: User, cfg: ChainSettings,
                    chain: ChainConfig, group: str) -> None:
    """Перерисовывает то же меню, из которого нажали кнопку."""
    from sniperbot.bot.views import render_main  # локальный импорт: избегаем цикла

    text = (callback.message.text or "") if callback.message else ""
    if text.startswith("🤖"):
        async with session_scope() as session:
            count = await repo.count_open_positions(session, user.id, chain.key)
        await safe_edit(callback, await render_main(ctx, user, cfg, chain, count),
                        main_menu(chain.name, cfg.auto_snipe))
        return
    title = GROUPS.get(group, group)
    await safe_edit(callback, f"{title} — {esc(chain.name)}",
                    group_menu(group, cfg, chain.native_symbol, user))


def _header(chain: ChainConfig) -> str:
    return (
        f"⚙️ <b>Настройки</b> — {esc(chain.name)}\n\n"
        "Настройки торговли хранятся отдельно для каждой сети.\n"
        "Текстом: <code>/config</code> — весь список, <code>/set имя значение</code> — изменить."
    )
