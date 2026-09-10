"""Старт, главное меню, помощь и переключение сети."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.keyboards import ChainCB, MenuCB, back_button, chains_menu, main_menu
from sniperbot.bot.texts import DISCLAIMER, HELP, WELCOME
from sniperbot.bot.ui import reply, safe_edit
from sniperbot.bot.views import render_main
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.fees import parse_referral

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(
    message: Message, command: CommandObject, ctx: BotContext, user: User,
    cfg: ChainSettings, chain: ChainConfig, is_new_user: bool, state: FSMContext,
) -> None:
    await state.clear()
    invited_by = await _accept_referral(user, command.args)
    if is_new_user:
        text = (WELCOME
                + f"💼 Ваш кошелёк создан:\n<code>{user.wallet_address}</code>\n\n"
                + "Пополните его, чтобы начать торговать.\n\n")
        if invited_by:
            text += "Вы пришли по приглашению — спасибо тому, кто позвал.\n\n"
        await reply(message, text + DISCLAIMER)
    await show_main(message, ctx, user, cfg, chain)


async def _accept_referral(user: User, payload: str | None) -> int | None:
    """Запоминает пригласившего из ссылки-приглашения.

    Привязка делается один раз и только при первом заходе: иначе приглашённого
    можно было бы «перепривязать» и накрутить себе бесплатные пополнения.
    """
    referrer = parse_referral(payload)
    if referrer is None or referrer == user.id or user.referred_by is not None:
        return None
    async with session_scope() as session:
        if not await repo.set_referrer(session, user.id, referrer):
            return None
    user.referred_by = referrer
    return referrer


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await reply(message, HELP, back_button())


@router.message(Command("menu"))
async def cmd_menu(message: Message, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig) -> None:
    await show_main(message, ctx, user, cfg, chain)


@router.callback_query(MenuCB.filter(F.section == "main"))
async def cb_main(
    callback: CallbackQuery, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig,
    state: FSMContext,
) -> None:
    await state.clear()
    async with session_scope() as session:
        count = await repo.count_open_positions(session, user.id, chain.key)
    text = await render_main(ctx, user, cfg, chain, count)
    await safe_edit(callback, text, main_menu(chain.name, cfg.auto_snipe))
    await callback.answer()


@router.callback_query(MenuCB.filter(F.section == "help"))
async def cb_help(callback: CallbackQuery) -> None:
    await safe_edit(callback, HELP, back_button())
    await callback.answer()


@router.message(Command("chain"))
async def cmd_chain(message: Message, ctx: BotContext, chain: ChainConfig) -> None:
    await reply(message, "🌐 Выберите сеть:", chains_menu(ctx.registry.configs, chain.key))


@router.callback_query(MenuCB.filter(F.section == "chains"))
async def cb_chains(callback: CallbackQuery, ctx: BotContext, chain: ChainConfig) -> None:
    await safe_edit(callback, "🌐 Выберите сеть:", chains_menu(ctx.registry.configs, chain.key))
    await callback.answer()


@router.callback_query(ChainCB.filter())
async def cb_switch_chain(
    callback: CallbackQuery, callback_data: ChainCB, ctx: BotContext, user: User
) -> None:
    config = ctx.registry.configs.get(callback_data.key)
    if config is None or not (config.enabled and config.configured):
        await callback.answer("Эта сеть пока не настроена", show_alert=True)
        return

    async with session_scope() as session:
        stored = await session.get(User, user.id)
        if stored is not None:
            stored.active_chain = callback_data.key
        cfg = await repo.get_settings(session, user.id, callback_data.key)
        count = await repo.count_open_positions(session, user.id, callback_data.key)
        user.active_chain = callback_data.key

    text = await render_main(ctx, user, cfg, config, count)
    await safe_edit(callback, text, main_menu(config.name, cfg.auto_snipe))
    await callback.answer(f"Сеть: {config.name}")


async def show_main(
    message: Message, ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig
) -> None:
    async with session_scope() as session:
        count = await repo.count_open_positions(session, user.id, chain.key)
    text = await render_main(ctx, user, cfg, chain, count)
    await reply(message, text, main_menu(chain.name, cfg.auto_snipe))
