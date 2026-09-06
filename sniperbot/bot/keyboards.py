"""Инлайн-клавиатуры и callback-данные."""

from __future__ import annotations

from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

QUICK_AMOUNTS = ("0.01", "0.05", "0.1", "0.5")


class MenuCB(CallbackData, prefix="m"):
    section: str


class ChainCB(CallbackData, prefix="ch"):
    key: str


class WalletCB(CallbackData, prefix="w"):
    action: str


class BuyCB(CallbackData, prefix="b"):
    token: str
    amount: str


class PosCB(CallbackData, prefix="p"):
    action: str
    pid: int
    pct: int = 0


class SetCB(CallbackData, prefix="s"):
    action: str
    field: str = ""


def main_menu(chain_name: str, auto_snipe: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Кошелёк", callback_data=MenuCB(section="wallet"))
    kb.button(text="📊 Позиции", callback_data=MenuCB(section="positions"))
    kb.button(
        text=("🎯 Автоснайп: ВКЛ" if auto_snipe else "🎯 Автоснайп: выкл"),
        callback_data=SetCB(action="toggle", field="auto_snipe"),
    )
    kb.button(text="⚙️ Настройки", callback_data=MenuCB(section="settings"))
    kb.button(text=f"🌐 Сеть: {chain_name}", callback_data=MenuCB(section="chains"))
    kb.button(text="ℹ️ Помощь", callback_data=MenuCB(section="help"))
    kb.adjust(2, 2, 2)
    return kb.as_markup()


def back_button(section: str = "main") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=MenuCB(section=section))
    return kb.as_markup()


def wallet_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=WalletCB(action="refresh"))
    kb.button(text="📥 Пополнить", callback_data=WalletCB(action="deposit"))
    kb.button(text="📤 Вывести", callback_data=WalletCB(action="withdraw"))
    kb.button(text="🧾 История", callback_data=WalletCB(action="history"))
    kb.button(text="🔐 Приватный ключ", callback_data=WalletCB(action="export"))
    kb.button(text="⬅️ Назад", callback_data=MenuCB(section="main"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def confirm_export() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Показать ключ", callback_data=WalletCB(action="export_confirm"))
    kb.button(text="❌ Отмена", callback_data=MenuCB(section="wallet"))
    kb.adjust(1)
    return kb.as_markup()


def chains_menu(chains: dict, active: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, config in chains.items():
        mark = "✅ " if key == active else ""
        status = "" if config.enabled and config.configured else " (не настроена)"
        kb.button(text=f"{mark}{config.name}{status}", callback_data=ChainCB(key=key))
    kb.button(text="⬅️ Назад", callback_data=MenuCB(section="main"))
    kb.adjust(1)
    return kb.as_markup()


def buy_menu(token: str, symbol: str, default_amount: Decimal, native: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⚡️ Купить на {default_amount} {native}", callback_data=BuyCB(token=token, amount="default"))
    for amount in QUICK_AMOUNTS:
        kb.button(text=f"{amount} {native}", callback_data=BuyCB(token=token, amount=amount))
    kb.button(text="✏️ Своя сумма", callback_data=BuyCB(token=token, amount="custom"))
    kb.button(text="🔄 Перепроверить", callback_data=BuyCB(token=token, amount="recheck"))
    kb.button(text="⬅️ Меню", callback_data=MenuCB(section="main"))
    kb.adjust(1, 4, 2, 1)
    return kb.as_markup()


def positions_list(positions) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for position in positions:
        kb.button(
            text=f"#{position.id} {position.token_symbol}",
            callback_data=PosCB(action="view", pid=position.id),
        )
    kb.button(text="🔄 Обновить", callback_data=MenuCB(section="positions"))
    kb.button(text="⬅️ Назад", callback_data=MenuCB(section="main"))
    kb.adjust(2)
    return kb.as_markup()


def position_actions(position_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for pct in (25, 50, 100):
        kb.button(text=f"Продать {pct}%", callback_data=PosCB(action="sell", pid=position_id, pct=pct))
    kb.button(text="🔄 Обновить", callback_data=PosCB(action="view", pid=position_id))
    kb.button(text="⬅️ К позициям", callback_data=MenuCB(section="positions"))
    kb.adjust(3, 2)
    return kb.as_markup()


def settings_menu(cfg, native: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💰 Сумма покупки: {cfg.buy_amount} {native}", callback_data=SetCB(action="edit", field="buy_amount"))
    kb.button(text=f"📉 Проскальзывание: {cfg.slippage_bps / 100:g}%", callback_data=SetCB(action="edit", field="slippage_bps"))
    kb.button(text=f"⛽️ Газ ×{cfg.gas_multiplier:g}", callback_data=SetCB(action="edit", field="gas_multiplier_bps"))
    kb.button(text=f"🎯 Тейк-профит: +{cfg.take_profit_pct}%", callback_data=SetCB(action="edit", field="take_profit_pct"))
    kb.button(text=f"🛑 Стоп-лосс: −{cfg.stop_loss_pct}%", callback_data=SetCB(action="edit", field="stop_loss_pct"))
    kb.button(text=f"📉 Трейлинг: {cfg.trailing_stop_pct or '—'}%", callback_data=SetCB(action="edit", field="trailing_stop_pct"))
    kb.button(text=f"🤖 Автопродажа: {_onoff(cfg.auto_sell)}", callback_data=SetCB(action="toggle", field="auto_sell"))
    kb.button(text=f"🎯 Автоснайп: {_onoff(cfg.auto_snipe)}", callback_data=SetCB(action="toggle", field="auto_snipe"))
    kb.button(text="🛡 Фильтры безопасности", callback_data=MenuCB(section="filters"))
    kb.button(text="⬅️ Назад", callback_data=MenuCB(section="main"))
    kb.adjust(1, 2, 3, 2, 1, 1)
    return kb.as_markup()


def filters_menu(cfg, native: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"💧 Мин. ликвидность: {cfg.min_liquidity} {native}", callback_data=SetCB(action="edit", field="min_liquidity"))
    kb.button(text=f"💧 Макс. ликвидность: {cfg.max_liquidity or '—'}", callback_data=SetCB(action="edit", field="max_liquidity"))
    kb.button(text=f"🧾 Макс. налог покупки: {cfg.max_buy_tax_bps / 100:g}%", callback_data=SetCB(action="edit", field="max_buy_tax_bps"))
    kb.button(text=f"🧾 Макс. налог продажи: {cfg.max_sell_tax_bps / 100:g}%", callback_data=SetCB(action="edit", field="max_sell_tax_bps"))
    kb.button(text=f"🍯 Проверка honeypot: {_onoff(cfg.honeypot_check)}", callback_data=SetCB(action="toggle", field="honeypot_check"))
    kb.button(text=f"🔬 Требовать симуляцию: {_onoff(cfg.require_simulation)}", callback_data=SetCB(action="toggle", field="require_simulation"))
    kb.button(text=f"👑 Только renounced: {_onoff(cfg.require_renounced)}", callback_data=SetCB(action="toggle", field="require_renounced"))
    kb.button(text=f"🔥 Мин. сожжённый LP: {cfg.min_lp_burned_pct}%", callback_data=SetCB(action="edit", field="min_lp_burned_pct"))
    kb.button(text=f"📦 Макс. позиций: {cfg.max_positions}", callback_data=SetCB(action="edit", field="max_positions"))
    kb.button(text=f"⏱ Снайпов в час: {cfg.max_snipes_per_hour}", callback_data=SetCB(action="edit", field="max_snipes_per_hour"))
    kb.button(text="⬅️ Настройки", callback_data=MenuCB(section="settings"))
    kb.adjust(2, 2, 1, 1, 1, 1, 2, 1)
    return kb.as_markup()


def cancel_kb(section: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=MenuCB(section=section).pack())]]
    )


def _onoff(value: bool) -> str:
    return "вкл" if value else "выкл"
