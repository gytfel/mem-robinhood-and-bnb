"""Настройки: экраны, кнопки, /config и /set."""

from __future__ import annotations

import logging

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
from sniperbot.settings_registry import GROUPS, SETTINGS, by_group, find
from sniperbot.utils.fmt import esc

log = logging.getLogger(__name__)

router = Router(name="settings")


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
    raw = (command.args or "").strip().lower() == "all"
    native = chain.native_symbol
    if raw:
        lines = [f"<b>Настройки</b> — {esc(chain.name)}\n<pre>"]
        for setting in SETTINGS:
            lines.append(f"{setting.name:<10} {setting.display(cfg, user, native)}")
        lines.append("</pre>")
        await reply(message, "\n".join(lines))
        return

    lines = [f"⚙️ <b>Настройки</b> — {esc(chain.name)}\n"]
    for group, title in GROUPS.items():
        items = by_group().get(group, [])
        if not items:
            continue
        lines.append(f"\n<b>{title}</b>")
        for setting in items:
            lines.append(
                f"<code>{setting.name}</code> = <b>{esc(setting.display(cfg, user, native))}</b>"
                f"\n    <i>{esc(setting.hint)}</i>"
            )
    lines.append("\nИзменить: <code>/set имя значение</code>, например <code>/set tp 150</code>")
    await reply(message, "\n".join(lines), settings_menu(cfg, native, user))


@router.message(Command("set"))
async def cmd_set(message: Message, command: CommandObject, cfg: ChainSettings,
                  chain: ChainConfig, user: User) -> None:
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
    await reply(
        message,
        f"✅ <b>{esc(setting.title)}</b> = {esc(setting.display(cfg, user, chain.native_symbol))}"
        + ("\n<i>Настройка общая для всех сетей</i>" if setting.scope == "user"
           else f"\n<i>Только для сети {esc(chain.name)}</i>"),
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
