"""Рендеринг карточек: главное меню, кошелёк, отчёт по токену, позиция."""

from __future__ import annotations

from decimal import Decimal

from sniperbot.bot.context import BotContext
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, Position, User
from sniperbot.fees import referral_progress, status_for
from sniperbot.sniper.safety import SafetyReport
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, short_addr

VERDICT_LABEL = {
    "safe": "🟢 Явных проблем не найдено",
    "risky": "🟠 Есть риски — решайте сами",
    "danger": "🔴 Опасно — покупка не рекомендуется",
}


async def referral_line(ctx: BotContext, user: User) -> str:
    """Сколько друзей нужно пригласить и сколько осталось — одной строкой.

    Показывается там, где человек видит свои деньги: условие акции бесполезно,
    если за ним нужно идти в отдельную команду.
    """
    policy = ctx.fees.policy()
    if not policy.enabled:
        return ""
    async with session_scope() as session:
        referrals = await repo.referral_count(session, user.id)
    status = status_for(
        referrals=referrals,
        is_admin=user.id in ctx.settings.admin_ids,
        exempt=bool(user.fee_exempt),
        policy=policy,
    )
    line = referral_progress(status)
    return f"\n{line}\nВаша ссылка: /ref" if line else ""


async def render_main(ctx: BotContext, user: User, cfg: ChainSettings, chain: ChainConfig, open_positions: int) -> str:
    balance = await _safe_balance(ctx, chain.key, user.wallet_address)
    return (
        f"🤖 <b>Memecoin Sniper</b>\n\n"
        f"🌐 Сеть: <b>{esc(chain.name)}</b>\n"
        f"💼 Кошелёк: <code>{user.wallet_address}</code>\n"
        f"💰 Баланс: <b>{balance}</b>\n"
        f"📊 Открытых позиций: <b>{open_positions}</b>\n"
        f"🎯 Автоснайп: <b>{'включён' if cfg.auto_snipe else 'выключен'}</b>\n"
        f"💵 Сумма покупки: <b>{fmt_amount(cfg.buy_amount)} {chain.native_symbol}</b>\n"
        + await referral_line(ctx, user)
        + "\nПришлите адрес токена, чтобы проверить и купить его."
    )


async def render_wallet(ctx: BotContext, user: User, chain: ChainConfig) -> str:
    lines = [
        "💼 <b>Ваш кошелёк</b>\n",
        f"<code>{user.wallet_address}</code>\n",
        "<b>Балансы</b>",
    ]
    for key in ctx.active_chain_keys:
        cfg = ctx.chain(key)
        balance = await _safe_balance(ctx, key, user.wallet_address)
        mark = "▸" if key == chain.key else "·"
        lines.append(f"{mark} {esc(cfg.name)}: <b>{balance}</b>")
    lines.append(
        f"\n📥 Для пополнения отправьте <b>{chain.native_symbol}</b> на адрес выше "
        f"(сеть {esc(chain.name)}).\n"
        "Адрес одинаковый во всех EVM-сетях бота — не отправляйте монеты других блокчейнов."
    )
    # Комиссия снимается именно с пополнения, поэтому условие её отмены должно
    # стоять на том же экране, а не в отдельной команде.
    fees = await _deposit_fee_line(ctx, user, chain)
    if fees:
        lines.append(fees)
    return "\n".join(lines)


async def _deposit_fee_line(ctx: BotContext, user: User, chain: ChainConfig) -> str:
    policy = ctx.fees.policy()
    if not policy.enabled:
        return ""
    async with session_scope() as session:
        referrals = await repo.referral_count(session, user.id)
    status = status_for(
        referrals=referrals,
        is_admin=user.id in ctx.settings.admin_ids,
        exempt=bool(user.fee_exempt),
        policy=policy,
    )
    progress = referral_progress(status)
    if status.free_deposit:
        # Достигнутую цель строка прогресса называет сама — второй раз не повторяем.
        return f"\n{progress}" if progress else "\n✅ Пополнения без комиссии."
    head = f"🧾 Комиссия за пополнение: <b>{status.deposit_pct:g}%</b>."
    return f"\n{head}\n{progress}\nВаша ссылка: /ref" if progress else f"\n{head}"


def render_report(report: SafetyReport, chain: ChainConfig) -> str:
    token = report.token
    lines = [
        f"🔎 <b>{esc(token.symbol)}</b> — {esc(token.name)}",
        f"<code>{token.address}</code>",
        f"🌐 {esc(chain.name)} · <a href='{chain.token_url(token.address)}'>обозреватель</a>",
    ]
    if report.venue:
        lines.append(f"🏦 Площадка: {esc(report.venue)}")
    if report.pair_state:
        lines.append(
            f"💧 Ликвидность: <b>{fmt_amount(report.liquidity_native, 4)} {chain.native_symbol}</b>"
        )
        lines.append(f"💱 Цена: {fmt_amount(report.pair_state.price_native, 12)} {chain.native_symbol}")
    if report.buy_tax_pct is not None or report.sell_tax_pct is not None:
        lines.append(
            f"🧾 Налоги: покупка <b>{_pct(report.buy_tax_pct)}</b> / продажа <b>{_pct(report.sell_tax_pct)}</b>"
        )
    if report.lp_burned is not None:
        lines.append(f"🔥 LP сожжён: {report.lp_burned:.1f}%")
    lines.append(f"👑 Владелец: {'renounced' if token.renounced else esc(token.owner or 'неизвестен')}")

    lines.append("\n<b>Проверки</b>")
    for check in report.checks:
        detail = f" — {esc(check.detail)}" if check.detail else ""
        lines.append(f"{check.icon} {esc(check.title)}{detail}")

    lines.append(f"\n<b>Итог:</b> {VERDICT_LABEL[report.verdict]} ({report.score}/100)")
    if report.blocking:
        lines.append("Блокирующие проблемы: " + esc(", ".join(c.title for c in report.blocking)))
    return "\n".join(lines)


def render_position(position: Position, chain: ChainConfig, price: Decimal | None) -> str:
    tokens = from_wei(position.amount_wei, position.token_decimals)
    spent = from_wei(position.native_spent_wei, chain.native_decimals)
    returned = from_wei(position.native_returned_wei, chain.native_decimals)
    value = (price * tokens) if price is not None else None
    pnl = None
    if value is not None and spent > 0:
        pnl = ((value + returned) / spent - 1) * 100

    lines = [
        f"📊 <b>Позиция #{position.id}</b> — {esc(position.token_symbol)}",
        f"<code>{position.token_address}</code>",
        f"🌐 {esc(chain.name)} · статус: {'открыта' if position.is_open else 'закрыта'}",
        f"📦 Остаток: {fmt_amount(tokens, 4)} {esc(position.token_symbol)}",
        f"💸 Вложено: {fmt_amount(spent)} {chain.native_symbol}",
    ]
    if returned > 0:
        lines.append(f"💵 Возвращено: {fmt_amount(returned)} {chain.native_symbol}")
    if value is not None:
        lines.append(f"💰 Текущая стоимость: {fmt_amount(value)} {chain.native_symbol}")
    if pnl is not None:
        icon = "🟢" if pnl >= 0 else "🔴"
        lines.append(f"{icon} P&L: <b>{pnl:+.1f}%</b>")
    if position.entry_price:
        lines.append(f"🎯 Вход: {fmt_amount(position.entry_price, 12)} {chain.native_symbol}")
    if price is not None:
        lines.append(f"💱 Сейчас: {fmt_amount(price, 12)} {chain.native_symbol}")
    lines.append("🤖 Автовыход: " + exit_rules(position))
    if position.buy_tx:
        lines.append(f"<a href='{chain.tx_url(position.buy_tx)}'>Транзакция покупки</a>")
    return "\n".join(lines)


def exit_rules(position: Position) -> str:
    """Правила выхода этой позиции — те, что записаны в ней самой.

    Позиция живёт по снимку настроек на момент покупки, поэтому показывать надо
    именно его: человек, поменявший тейк вчера, должен видеть здесь старое
    правило, а не новое, и понимать, что нужен /apply.
    """
    from sniperbot.settings_registry import ladder_steps, step_multiplier

    if not position.auto_sell:
        return "выключен"

    rules = []
    done = {step.strip() for step in (position.tp_done or "").split(",") if step.strip()}
    steps = ladder_steps(position.tp_ladder)
    if steps:
        shown = [f"×{step_multiplier(growth)}→{share}%" + ("✅" if str(growth) in done else "")
                 for growth, share in steps]
        rules.append("TP " + " · ".join(shown))
    elif position.take_profit_pct:
        share = position.sell_percent or 100
        rules.append(f"TP ×{step_multiplier(position.take_profit_pct)}"
                     + (f" (продать {share}%)" if share < 100 else " (продать всё)")
                     + ("✅" if "tp" in done else ""))
    if position.secure_pct:
        rules.append(f"возврат вложенного ×{step_multiplier(int(position.secure_pct))}"
                     + ("✅" if "secure" in done else ""))
    if position.stop_loss_pct:
        rules.append(f"SL −{position.stop_loss_pct}%")
    if position.breakeven_armed:
        rules.append("стоп в безубытке")
    if position.trailing_stop_pct:
        rules.append(f"трейлинг {position.trailing_stop_pct}%")
    return ", ".join(rules) if rules else "выключен"


def render_positions_list(positions: list[Position], chain_names: dict[str, str]) -> str:
    if not positions:
        return "📊 Открытых позиций нет.\n\nПришлите адрес токена, чтобы купить первый."
    lines = ["📊 <b>Открытые позиции</b>\n"]
    for position in positions:
        pnl = ""
        if position.entry_price and position.last_price and position.entry_price > 0:
            change = (position.last_price / position.entry_price - 1) * 100
            pnl = f" · {'🟢' if change >= 0 else '🔴'} {change:+.1f}%"
        lines.append(
            f"#{position.id} <b>{esc(position.token_symbol)}</b> "
            f"({esc(chain_names.get(position.chain, position.chain))}){pnl}\n"
            f"   {short_addr(position.token_address)} · "
            f"{fmt_amount(from_wei(position.native_spent_wei))} вложено"
        )
    return "\n".join(lines)


async def _safe_balance(ctx: BotContext, chain_key: str, address: str | None) -> str:
    if not address:
        return "—"
    chain = ctx.chain(chain_key)
    try:
        raw = await ctx.registry.get(chain_key).native_balance(address)
    except Exception:  # noqa: BLE001 - RPC может лежать, интерфейс не должен падать
        return f"недоступно ({chain.native_symbol})"
    return f"{fmt_amount(from_wei(raw, chain.native_decimals))} {chain.native_symbol}"


def _pct(value) -> str:
    return "—" if value is None else f"{value:.1f}%"
