"""Команды администратора: здоровье, расход ресурсов, пользователи, рассылка."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import signal
import time
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from sniperbot.access import OPEN, PRIVATE, STATE_EXTRA, STATE_MODE
from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.chain.clients import ChainClient
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import Position, SeenPair, TradeLog
from sniperbot.utils.fmt import esc, fmt_amount, from_wei

log = logging.getLogger(__name__)

router = Router(name="admin")


ADMIN_ONLY = ("🔒 Команда только для администратора бота.\n"
              "Список доступных вам команд: /help")


def _deny(is_admin: bool) -> bool:
    return not is_admin


async def _refuse(message: Message) -> None:
    """Отказ вслух: молчание выглядит поломкой, а не запретом."""
    await reply(message, ADMIN_ONLY)


@router.message(Command("stats_admin", "adminstats"))
@router.message(Command("health"))
async def cmd_health(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    from sniperbot.bot.startup import human_duration
    from sniperbot.db.models import BotRun
    from sniperbot.version import build_info

    async with session_scope() as session:
        runs = list((await session.scalars(
            select(BotRun).order_by(BotRun.id.desc()).limit(3))).all())

    lines = [f"🩺 <b>Здоровье бота</b>\n{esc(build_info().short())}"]
    if runs:
        current = runs[0]
        uptime = dt.datetime.now(dt.UTC) - _aware(current.started_at)
        unclean = sum(1 for run in runs[1:] if not run.clean_shutdown)
        lines.append(f"Аптайм: <b>{human_duration(uptime)}</b>"
                     + (f" · аварийных завершений подряд: {unclean}" if unclean else ""))
    lines.append("\n<b>Сканеры</b>")
    statuses = ctx.engine.status()
    if not statuses:
        lines.append("  ⛔️ ни один сканер не запущен — проверьте настройки сетей")
    for status in statuses:
        icon = "✅" if status["running"] else "⛔️"
        error = f" — {esc(status['error'])}" if status["error"] else ""
        lines.append(f"  {icon} {esc(status['name'])}{error}")

    lines.append("\n<b>Сети</b>")
    for key in ctx.active_chain_keys:
        chain = ctx.chain(key)
        client = ctx.registry.get(key)
        started = time.perf_counter()
        try:
            block = await client.block_number()
            latency = (time.perf_counter() - started) * 1000
            lines.append(f"  ✅ {esc(chain.name)}: блок {block}, {latency:.0f} мс")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  ⛔️ {esc(chain.name)}: {esc(str(exc)[:80])}")
            continue
        async with session_scope() as session:
            recent = await repo.recent_pairs(session, key, limit=1)
        if recent:
            age = dt.datetime.now(dt.UTC) - _aware(recent[0].created_at)
            lines.append(f"     последний пул: {int(age.total_seconds() // 60)} мин назад")
        else:
            lines.append("     новых пулов ещё не видел")

    async with session_scope() as session:
        open_positions = await session.scalar(
            select(func.count()).select_from(Position).where(Position.status == "open")
        )
    lines.append(f"\nОткрытых позиций у всех: <b>{open_positions}</b>")
    await reply(message, "\n".join(lines))


@router.message(Command("usage"))
async def cmd_usage(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    lines = ["📊 <b>Расход ресурсов</b>\n", "<b>Запросы к RPC</b> (с момента запуска)"]
    for key in ctx.active_chain_keys:
        client = ctx.registry.get(key)
        lines.append(f"  {esc(ctx.chain(key).name)}: {client.requests} запросов, "
                     f"сбоев {client.failures}, активный узел {esc(client.rpc_url)}")

    async with session_scope() as session:
        users = await repo.user_count(session)
        positions = await session.scalar(select(func.count()).select_from(Position))
        pairs = await session.scalar(select(func.count()).select_from(SeenPair))
        trades = await session.scalar(select(func.count()).select_from(TradeLog))

    lines.append(f"\n<b>База</b>\n  пользователей {users} · позиций {positions} · "
                 f"пулов {pairs} · сделок {trades}")
    db_path = _database_path(ctx.settings.resolved_database_url)
    if db_path and db_path.exists():
        size = db_path.stat().st_size / 1024 / 1024
        lines.append(f"  файл: {esc(str(db_path))} — {size:.1f} МБ")
    await reply(message, "\n".join(lines))


@router.message(Command("latency"))
async def cmd_latency(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    status = await reply(message, "⏱ Замеряю задержки эндпоинтов…")
    lines = ["⏱ <b>Задержки RPC</b>"]
    for key in ctx.active_chain_keys:
        chain: ChainConfig = ctx.chain(key)
        lines.append(f"\n<b>{esc(chain.name)}</b>")
        for url in chain.rpc_urls:
            single = ChainClient(_single_rpc(chain, url))
            started = time.perf_counter()
            try:
                await single.block_number()
                lines.append(f"  ✅ {esc(url)} — {(time.perf_counter() - started) * 1000:.0f} мс")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"  ⛔️ {esc(url)} — {esc(str(exc)[:60])}")
            finally:
                await single.close()
    lines.append("\nБот сам переключается на живой узел; медленные лучше убрать из .env.")
    await status.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("users"))
async def cmd_users(message: Message, ctx: BotContext, is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    async with session_scope() as session:
        users = await repo.list_users(session, limit=40)
        total = await repo.user_count(session)
        rows = []
        for user in users:
            open_count = await session.scalar(
                select(func.count()).select_from(Position).where(
                    Position.user_id == user.id, Position.status == "open")
            )
            rows.append((user, int(open_count or 0)))

    lines = [f"👥 <b>Пользователи</b>: {total}\n"]
    for user, open_count in rows:
        name = f"@{user.username}" if user.username else str(user.id)
        flags = []
        if user.is_blocked:
            flags.append("🚫")
        if user.dry_run:
            flags.append("🧪")
        lines.append(f"{' '.join(flags)} <code>{user.id}</code> {esc(name)} · позиций {open_count}")
    lines.append("\nКарточка: <code>/userinfo ID</code>")
    await reply(message, "\n".join(lines))


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, command: CommandObject, ctx: BotContext,
                       is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await reply(message, "Использование: <code>/userinfo ID</code>")
        return

    user_id = int(raw)
    async with session_scope() as session:
        user = await repo.get_user(session, user_id)
        if user is None:
            await reply(message, "Пользователь не найден.")
            return
        open_positions = await repo.open_positions(session, user_id=user_id)
        spent, returned = await repo.total_pnl(session, user_id)

    lines = [
        f"👤 <b>{esc('@' + user.username if user.username else user_id)}</b>",
        f"ID: <code>{user.id}</code>",
        f"Кошелёк: <code>{user.wallet_address}</code>",
        f"Сеть: {esc(user.active_chain)} · режим: {'🧪 тест' if user.dry_run else '💰 боевой'}",
        f"Статус: {'🚫 заблокирован' if user.is_blocked else '✅ активен'}",
        f"Открытых позиций: {len(open_positions)}",
        f"Итог по закрытым: {fmt_amount(from_wei(returned - spent))}",
        f"Создан: {user.created_at:%d.%m.%Y}",
    ]
    for key in ctx.active_chain_keys:
        try:
            balance = await ctx.registry.get(key).native_balance(user.wallet_address)
            lines.append(f"{esc(ctx.chain(key).name)}: {fmt_amount(from_wei(balance))} "
                         f"{ctx.chain(key).native_symbol}")
        except Exception:  # noqa: BLE001
            continue
    await reply(message, "\n".join(lines))


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject, is_admin: bool = False) -> None:
    await _set_block(message, command, is_admin, True)


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject, is_admin: bool = False) -> None:
    await _set_block(message, command, is_admin, False)


async def _set_block(message: Message, command: CommandObject, is_admin: bool, blocked: bool) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await reply(message, f"Использование: <code>/{'ban' if blocked else 'unban'} ID</code>")
        return
    async with session_scope() as session:
        found = await repo.set_blocked(session, int(raw), blocked)
    if not found:
        await reply(message, "Пользователь не найден.")
        return
    await reply(message, f"{'🚫 Заблокирован' if blocked else '✅ Разблокирован'}: <code>{raw}</code>"
                         + ("\nАвтоснайп для него остановлен, позиции остаются." if blocked else ""))


@router.message(Command("access"))
async def cmd_access(message: Message, command: CommandObject, ctx: BotContext,
                     is_admin: bool = False) -> None:
    """Открыть бота всем или вернуть белый список — без правки .env и рестарта."""
    if _deny(is_admin):
        await _refuse(message)
        return

    policy = ctx.access
    parts = (command.args or "").strip().lower().split()
    action = parts[0] if parts else ""
    argument = parts[1] if len(parts) > 1 else ""

    if action in {"open", "всем", "открыть"}:
        policy.override = OPEN
        await _save_access(policy)
        await reply(message, "🔓 <b>Бот открыт для всех.</b>\n"
                             "Любой, кто нажмёт /start, получит кошелёк и сможет торговать.\n"
                             "Закрыть обратно: <code>/access private</code>")
        return

    if action in {"private", "closed", "закрыть"}:
        policy.override = PRIVATE
        await _save_access(policy)
        await reply(message, "🔒 <b>Бот закрыт.</b> Доступ только у администраторов и "
                             f"белого списка ({len(policy.allowed_ids())} чел.).\n"
                             "Добавить: <code>/access add ID</code>")
        return

    if action in {"add", "del", "remove"} and argument.lstrip("-").isdigit():
        user_id = int(argument)
        if action == "add":
            changed = policy.add(user_id)
            text = ("✅ Добавлен в белый список" if changed else "Он уже в списке")
        else:
            changed = policy.remove(user_id)
            text = ("✅ Убран из белого списка" if changed else
                    "Его нет среди добавленных командой (список из .env отсюда не меняется)")
        if changed:
            await _save_access(policy)
        await reply(message, f"{text}: <code>{user_id}</code>\n{_access_status(policy)}")
        return

    if action:
        await reply(message, "Использование:\n"
                             "<code>/access open</code> — открыть бота всем\n"
                             "<code>/access private</code> — только белый список\n"
                             "<code>/access add ID</code> · <code>/access del ID</code>")
        return

    await reply(message, _access_status(policy))


def _access_status(policy) -> str:  # noqa: ANN001 - AccessPolicy
    lines = [f"👥 <b>Доступ к боту</b>: {'🔓 открыт всем' if policy.is_open else '🔒 по списку'}"]
    if policy.is_open:
        lines.append("Любой, кто нажмёт /start, получит кошелёк и сможет торговать.")
        lines.append("\nЗакрыть: <code>/access private</code>")
    else:
        allowed = policy.allowed_ids()
        lines.append(f"Кроме администраторов пускаем {len(allowed)} чел.")
        if allowed:
            lines.append(" ".join(f"<code>{uid}</code>" for uid in allowed[:20]))
        lines.append("\nОткрыть всем: <code>/access open</code> · "
                     "добавить: <code>/access add ID</code>")
    lines.append("Заблокировать отдельного: <code>/ban ID</code>")
    return "\n".join(lines)


async def _save_access(policy) -> None:  # noqa: ANN001 - AccessPolicy
    """Решение переживает перезапуск: .env остаётся нетронутым."""
    async with session_scope() as session:
        await repo.set_state(session, STATE_MODE, policy.override)
        await repo.set_state(session, STATE_EXTRA, policy.extra_value())


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, ctx: BotContext,
                        is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    text = (command.args or "").strip()
    if not text:
        await reply(message, "Использование: <code>/broadcast текст сообщения</code>")
        return

    async with session_scope() as session:
        users = await repo.all_users(session, with_wallet=False)

    sent = 0
    status = await reply(message, f"📢 Отправляю {len(users)} пользователям…")
    for user in users:
        await ctx.notifier.send(user.id, f"📢 <b>Сообщение от администратора</b>\n\n{esc(text)}")
        sent += 1
        await asyncio.sleep(0.05)      # ~20 сообщений в секунду — лимит Telegram
    await status.edit_text(f"📢 Отправлено: {sent} из {len(users)}", parse_mode="HTML")


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject, ctx: BotContext,
                   is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    limit = int(command.args) if (command.args or "").strip().isdigit() else 15
    async with session_scope() as session:
        trades = await repo.recent_trades(session, limit=max(1, min(50, limit)))

    if not trades:
        await reply(message, "🧾 Сделок в журнале пока нет.")
        return

    lines = ["🧾 <b>Журнал операций</b>\n"]
    for trade in trades:
        icon = {"success": "✅", "failed": "⛔️"}.get(trade.status, "⏳")
        chain = ctx.chain(trade.chain) if trade.chain in ctx.registry.configs else None
        link = f"<a href='{chain.tx_url(trade.tx_hash)}'>tx</a>" if chain and trade.tx_hash else ""
        error = f" — {esc(trade.error[:60])}" if trade.error else ""
        lines.append(
            f"{icon} {trade.created_at:%d.%m %H:%M} · {trade.kind} · "
            f"user {trade.user_id} · {esc((trade.token_address or '')[:10])} {link}{error}"
        )
    await reply(message, "\n".join(lines))


@router.message(Command("restart"))
async def cmd_restart(message: Message, command: CommandObject, ctx: BotContext,
                      is_admin: bool = False) -> None:
    if _deny(is_admin):
        await _refuse(message)
        return
    if (command.args or "").strip().lower() not in {"confirm", "да", "now"}:
        await reply(
            message,
            "♻️ <b>Перезапуск процесса</b>\n\n"
            "Бот корректно завершится, а systemd (или Docker) поднимет его заново — "
            "придёт обычное уведомление о старте. Открытые позиции сохранятся, "
            "но пока сервис перезапускается, автоснайп и стопы не работают.\n\n"
            "Подтвердить: <code>/restart confirm</code>\n\n"
            "<i>Если бот запущен вручную из терминала, он просто выключится.</i>",
        )
        return

    await reply(message, "♻️ Останавливаюсь. Если настроен автозапуск — вернусь через несколько секунд.")
    log.warning("Перезапуск по команде администратора %s", message.from_user.id)

    async def _shutdown() -> None:
        await asyncio.sleep(1)          # даём сообщению уйти
        signal.raise_signal(signal.SIGTERM)

    asyncio.create_task(_shutdown())


def _single_rpc(chain: ChainConfig, url: str) -> ChainConfig:
    """Копия конфига сети с единственным RPC — для честного замера."""
    from dataclasses import replace

    return replace(chain, rpc_urls=[url])


def _database_path(url: str) -> Path | None:
    marker = "sqlite+aiosqlite:///"
    if not url.startswith(marker):
        return None
    raw = url[len(marker):]
    return None if raw == ":memory:" else Path(raw)


def _aware(value):  # noqa: ANN001
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
