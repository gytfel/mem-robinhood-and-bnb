"""Исследовательские команды: A/B-тест, репутация создателей, тренды, маршруты."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from collections import Counter
from decimal import Decimal

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sniperbot.bot.context import BotContext
from sniperbot.bot.ui import reply
from sniperbot.chain.dex_adapter import adapters_for
from sniperbot.config import ChainConfig
from sniperbot.db import repo
from sniperbot.db.base import session_scope
from sniperbot.db.models import ChainSettings, User
from sniperbot.settings_registry import describe_variant, find, parse_variant
from sniperbot.utils.evm import extract_address
from sniperbot.utils.fmt import esc, fmt_amount, from_wei, short_addr

log = logging.getLogger(__name__)

router = Router(name="lab")

WORD_RE = re.compile(r"[a-zA-Zа-яА-Я][a-zA-Zа-яА-Я0-9]{2,}")


def _window(args: str | None) -> tuple[int | None, str]:
    """«12» → последние 12 часов; без аргумента — за всё время."""
    raw = (args or "").strip()
    if raw.isdigit():
        hours = max(1, min(8760, int(raw)))
        return hours, f"за {hours} ч"
    return None, "за всё время"
STOP_WORDS = {"token", "coin", "the", "inu", "finance", "protocol", "network", "official"}


# --------------------------------------------------------------------- A/B тест
@router.message(Command("ab"))
async def cmd_ab(message: Message, command: CommandObject, user: User,
                 cfg: ChainSettings, chain: ChainConfig) -> None:
    parts = (command.args or "").split()
    action = parts[0].lower() if parts else "status"

    if action == "set" and len(parts) >= 3:
        setting = find(parts[1])
        if setting is None or setting.scope != "chain":
            await reply(message, f"❌ Настройку «{esc(parts[1])}» в тесте менять нельзя. Список: /config")
            return
        try:
            setting.parse(parts[2])          # проверяем значение сразу
        except ValueError as exc:
            await reply(message, f"❌ {esc(setting.title)}: {esc(exc)}")
            return
        variant = parse_variant(cfg.ab_variant)
        variant[setting.name] = parts[2]
        await _save_ab(user.id, chain.key, cfg, enabled=True, variant=variant)
        await reply(
            message,
            f"🧬 Вариант B: {esc(describe_variant(variant))}\n\n"
            "Тест включён: половина автопокупок пойдёт с этими настройками, "
            "половина — с текущими. Сравнение: /ab",
        )
        return

    if action == "clear":
        await _save_ab(user.id, chain.key, cfg, enabled=False, variant={})
        await reply(message, "🧬 A/B-тест выключен, вариант очищен.")
        return

    if action == "apply":
        variant = parse_variant(cfg.ab_variant)
        if not variant:
            await reply(message, "Вариант B пуст — применять нечего.")
            return
        async with session_scope() as session:
            stored = await repo.get_settings(session, user.id, chain.key)
            for name, raw in variant.items():
                setting = find(name)
                if setting is not None:
                    setting.write(setting.parse(str(raw)), stored, None)
            stored.ab_enabled = False
            stored.ab_variant = ""
        await reply(message, f"✅ Настройки варианта B применены ко всем сделкам:\n"
                             f"{esc(describe_variant(variant))}\nТест выключен.")
        return

    # --- статус и сравнение ---
    variant = parse_variant(cfg.ab_variant)
    async with session_scope() as session:
        stats = await repo.ab_stats(session, user.id, chain.key)

    lines = [f"🧬 <b>A/B-тест настроек</b> — {esc(chain.name)}\n"]
    lines.append(f"Состояние: <b>{'включён' if cfg.ab_enabled and variant else 'выключен'}</b>")
    lines.append(f"Вариант B: {esc(describe_variant(variant))}\n")

    total = stats["A"]["trades"] + stats["B"]["trades"]
    if total:
        for group, title in (("A", "A (текущие настройки)"), ("B", "B (вариант)")):
            data = stats[group]
            if not data["trades"]:
                lines.append(f"<b>{title}</b>: сделок пока нет")
                continue
            winrate = data["wins"] * 100 // data["trades"]
            avg = Decimal(data["pnl"]) / data["trades"]
            lines.append(
                f"<b>{title}</b>: сделок {data['trades']}, прибыльных {winrate}%, "
                f"итог {fmt_amount(from_wei(data['pnl']))} "
                f"(в среднем {fmt_amount(from_wei(int(avg)))} на сделку)"
            )
        if stats["A"]["trades"] >= 5 and stats["B"]["trades"] >= 5:
            better = "B" if stats["B"]["pnl"] > stats["A"]["pnl"] else "A"
            lines.append(f"\nПока лучше группа <b>{better}</b>. "
                         "Меньше 20 сделок на группу — это ещё не вывод, а наблюдение.")
        else:
            lines.append("\nНужно хотя бы по 5 закрытых сделок в каждой группе для сравнения.")
    else:
        lines.append("Закрытых сделок в тесте пока нет.")

    lines.append(
        "\n<code>/ab set tp 300</code> — задать параметр варианта\n"
        "<code>/ab apply</code> — сделать вариант основным\n"
        "<code>/ab clear</code> — выключить тест"
    )
    await reply(message, "\n".join(lines))


async def _save_ab(user_id: int, chain_key: str, cfg: ChainSettings,
                   *, enabled: bool, variant: dict) -> None:
    payload = json.dumps(variant, ensure_ascii=False) if variant else ""
    async with session_scope() as session:
        stored = await repo.get_settings(session, user_id, chain_key)
        stored.ab_enabled = enabled
        stored.ab_variant = payload
    cfg.ab_enabled = enabled
    cfg.ab_variant = payload


# ------------------------------------------------------------ репутация создателей
@router.message(Command("creators"))
async def cmd_creators(message: Message, user: User, chain: ChainConfig) -> None:
    async with session_scope() as session:
        stats = await repo.creator_stats(session, user.id, chain.key)
        bad = await repo.bad_owners(session, user.id, chain.key)

    if not stats:
        await reply(
            message,
            "👤 <b>Создатели токенов</b>\n\nДанных пока нет: репутация набирается "
            "по закрытым сделкам. Владелец контракта, на котором вы потеряли, "
            "попадает в игнор автоснайпа (настройка <code>/set nobadcreators on</code>).",
        )
        return

    lines = [f"👤 <b>Создатели токенов</b> — {esc(chain.name)}\n"]
    for item in stats[:10]:
        pnl = from_wei(item["pnl"])
        icon = "🟢" if item["pnl"] > 0 else "🔴"
        blocked = " ⛔️ в игноре" if item["owner"] in bad else ""
        symbols = ", ".join(item["symbols"][:3])
        lines.append(
            f"{icon} <code>{short_addr(item['owner'])}</code>{blocked}\n"
            f"    сделок {item['trades']} · прибыльных {item['wins']} · "
            f"итог {fmt_amount(pnl)}" + (f" · {esc(symbols)}" if symbols else "")
        )
    lines.append(f"\nВ игноре сейчас: {len(bad)}. Отключить правило: "
                 "<code>/set nobadcreators off</code>")
    await reply(message, "\n".join(lines))


# --------------------------------------------------------------------- тренды
@router.message(Command("trends"))
async def cmd_trends(message: Message, command: CommandObject, ctx: BotContext,
                     chain: ChainConfig) -> None:
    hours, window = _window(command.args)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours) if hours else None

    async with session_scope() as session:
        pairs = await repo.pairs_since(session, chain.key, since)

    named = [pair for pair in pairs if pair.token_symbol or pair.token_name]
    if not named:
        await reply(
            message,
            f"🔥 <b>Тренды</b> {window}\n\nНазвания токенов пока не собраны. "
            "Они появляются, когда автоснайп разбирает новые пулы — включите /on "
            "(можно в тестовом режиме /dry).",
        )
        return

    words = Counter()
    for pair in named:
        text = f"{pair.token_symbol} {pair.token_name}".lower()
        for word in WORD_RE.findall(text):
            if word not in STOP_WORDS and len(word) >= 3:
                words[word] += 1

    lines = [f"🔥 <b>Горячие темы</b> {window} — {esc(chain.name)}",
             f"Разобрано новых токенов: {len(named)} из {len(pairs)}\n"]
    for word, count in words.most_common(12):
        if count < 2:
            continue
        lines.append(f"• <b>{esc(word)}</b> — {count}")
    if len(lines) == 3:
        lines.append("Повторяющихся тем нет — поток разрозненный.")
    lines.append("\nСузить период: <code>/trends 24</code> (часы)")
    await reply(message, "\n".join(lines))


# ------------------------------------------------------ конкуренция за вход
@router.message(Command("bundles", "competition"))
async def cmd_bundles(message: Message, command: CommandObject, chain: ChainConfig) -> None:
    hours, window = _window(command.args)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours) if hours else None

    async with session_scope() as session:
        pairs = await repo.pairs_since(session, chain.key, since)

    measured = [pair for pair in pairs if pair.first_block_swaps >= 0]
    if not measured:
        await reply(
            message,
            f"🏁 <b>Конкуренция за вход</b> {window}\n\nЗамеров пока нет: "
            "число сделок в первых блоках считается при разборе новых пулов автоснайпом.",
        )
        return

    crowded = [pair for pair in measured if pair.first_block_swaps >= 5]
    quiet = [pair for pair in measured if pair.first_block_swaps == 0]
    average = sum(pair.first_block_swaps for pair in measured) / len(measured)

    await reply(
        message,
        f"🏁 <b>Конкуренция за вход</b> {window} — {esc(chain.name)}\n\n"
        f"Замерено пулов: <b>{len(measured)}</b>\n"
        f"В среднем сделок в первых блоках: <b>{average:.1f}</b>\n"
        f"С толпой (5+ сделок сразу): <b>{len(crowded)}</b> "
        f"({len(crowded) * 100 // len(measured)}%)\n"
        f"Совсем без активности: <b>{len(quiet)}</b>\n\n"
        "<i>Много толпы — вы входите последним и покупаете уже дороже: "
        "поднимите газ (/set gasmode turbo) или ужесточьте фильтры, "
        "чтобы брать только то, что действительно того стоит.</i>",
    )


# ------------------------------------------------------------------ маршруты
@router.message(Command("paths"))
async def cmd_paths(message: Message, command: CommandObject, ctx: BotContext,
                    chain: ChainConfig) -> None:
    token = extract_address(command.args or "")
    if not token:
        await reply(message, "Использование: <code>/paths 0xАдресТокена</code>\n"
                             "Покажу все площадки этой сети, где токен можно торговать.")
        return

    status = await reply(message, "🔀 Ищу пулы на всех площадках…")
    client = ctx.registry.get(chain.key)
    lines = [f"🔀 <b>Маршруты</b> для <code>{token}</code>\n{esc(chain.name)}\n"]
    found = 0
    for adapter in adapters_for(client):
        try:
            pool = await adapter.find_pool(token)
            if pool is None:
                lines.append(f"· {esc(adapter.name)}: пула нет")
                continue
            state = await adapter.pool_state(token, pool)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"· {esc(adapter.name)}: ошибка — {esc(str(exc)[:60])}")
            continue
        found += 1
        lines.append(
            f"✅ <b>{esc(adapter.name)}</b> ({pool.label})\n"
            f"    ликвидность {fmt_amount(state.liquidity_native, 4)} {chain.native_symbol}\n"
            f"    <code>{pool.address}</code>"
        )

    lines.append(f"\nБот выбирает самый глубокий пул автоматически ({found} найдено). "
                 "Жёстко закрепить: <code>/set route v2</code> или <code>/set route v3</code>.")
    await status.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("route"))
async def cmd_route(message: Message, command: CommandObject, user: User,
                    cfg: ChainSettings, chain: ChainConfig) -> None:
    value = (command.args or "").strip().lower()
    setting = find("route")
    if not value:
        await reply(
            message,
            f"🔀 Маршрут сейчас: <b>{esc(cfg.dex_route)}</b>\n\n"
            "<code>/route auto</code> — самый ликвидный пул\n"
            "<code>/route v2</code> · <code>/route v3</code> — только эта версия\n"
            "Где торгуется конкретный токен: <code>/paths 0xТокен</code>",
        )
        return
    try:
        parsed = setting.parse(value)
    except ValueError as exc:
        await reply(message, f"❌ {esc(exc)}")
        return
    async with session_scope() as session:
        stored = await repo.get_settings(session, user.id, chain.key)
        stored.dex_route = parsed
    cfg.dex_route = parsed
    await reply(message, f"✅ Маршрут: <b>{esc(parsed)}</b> ({esc(chain.name)})")


# ------------------------------------------------------- диагностика и советы
@router.message(Command("calibrate"))
async def cmd_calibrate(message: Message, command: CommandObject, ctx: BotContext,
                        cfg: ChainSettings, chain: ChainConfig) -> None:
    token = extract_address(command.args or "")
    if not token:
        await reply(message, "Использование: <code>/calibrate 0xАдресТокена</code>\n"
                             "Прогоню полную симуляцию и покажу, что удалось измерить.")
        return

    status = await reply(message, "🔬 Прогоняю симуляцию…")
    from sniperbot.chain.dex_adapter import find_best_venue
    from sniperbot.sniper.analysis import profile_token
    from sniperbot.sniper.safety import HoneypotSimulator
    from sniperbot.utils.fmt import to_wei

    client = ctx.registry.get(chain.key)
    venue = await find_best_venue(client, token)
    if venue is None:
        await status.edit_text("❌ Пул с ликвидностью не найден.", parse_mode="HTML")
        return
    adapter, pool, state = venue

    from sniperbot.chain.erc20 import fetch_token

    token_info = await fetch_token(client, token)
    simulator = HoneypotSimulator(client, adapter, pool)
    override = await simulator.supports_override()
    slot = await simulator.find_balance_slot(token, simulator._probe_address()) if override else None
    result = await simulator.simulate(token, token_info.decimals,
                                      to_wei(cfg.buy_amount, chain.native_decimals))
    profile = await profile_token(client, token_info, pool_address=pool.address)

    await status.edit_text(
        f"🔬 <b>Диагностика</b> {esc(token_info.symbol)}\n"
        f"<code>{token}</code>\n\n"
        f"Площадка: {esc(adapter.name)} ({pool.label})\n"
        f"Ликвидность: {fmt_amount(state.liquidity_native, 4)} {chain.native_symbol}\n"
        f"state override: {'✅ поддерживается' if override else '⛔️ нет'}\n"
        f"Слот баланса: {slot[0] if slot else '—'}"
        + (" (Vyper)" if slot and slot[1] else "") + "\n"
        f"Покупка: {_mark(result.can_buy)} · продажа: {_mark(result.can_sell)}\n"
        f"Налоги: покупка {_pct(result.buy_tax_bps)} / продажа {_pct(result.sell_tax_bps)}\n"
        f"Права владельца: {esc(profile.describe())}\n"
        + (f"Доля владельца: {profile.owner_share:.1f}%\n" if profile.owner_share is not None else "")
        + (f"\n⚠️ {esc(result.error)}" if result.error else ""),
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.message(Command("tip", "gasadvice"))
async def cmd_tip(message: Message, ctx: BotContext, user: User,
                  cfg: ChainSettings, chain: ChainConfig) -> None:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
    async with session_scope() as session:
        total, failed = await repo.failed_trades(session, user.id, since, kind="buy")

    client = ctx.registry.get(chain.key)
    try:
        fees = await client.gas_fees(1.0)
        base = int(fees.get("gasPrice") or fees.get("maxFeePerGas") or 0) / 1e9
        network = f"{base:.2f} gwei"
    except Exception:  # noqa: BLE001
        network = "недоступна"

    share = (failed * 100 // total) if total else 0
    if not total:
        advice = "Сделок за неделю не было — оценивать нечего. Начните с /dry."
    elif share >= 30:
        advice = (f"Срывается {share}% покупок — вы не успеваете. "
                  "Попробуйте <code>/set gasmode turbo</code> и увеличьте "
                  "проскальзывание входа.")
    elif share >= 10:
        advice = (f"Срывается {share}% покупок. Стоит поднять газ до "
                  "<code>/set gasmode fast</code>.")
    else:
        advice = ("Покупки проходят стабильно — газ можно не поднимать. "
                  "Если хотите экономить: <code>/set gasmode normal</code>.")

    await reply(
        message,
        f"⛽️ <b>Газ и скорость</b> — {esc(chain.name)}\n\n"
        f"Цена газа в сети: <b>{network}</b>\n"
        f"Ваш режим: <b>{cfg.gas_mode}</b> · на выходе ×{cfg.exit_gas_boost_bps / 10_000:g}\n"
        f"Покупок за 7 дней: {total}, из них сорвалось: {failed}\n\n{advice}",
    )


def _mark(value: bool | None) -> str:
    return "✅" if value else ("⛔️" if value is False else "❔")


def _pct(bps: int | None) -> str:
    return "—" if bps is None else f"{bps / 100:.1f}%"


# --------------------------------------------------------- перехват разгона
@router.message(Command("trending"))
async def cmd_trending(message: Message, ctx: BotContext, cfg: ChainSettings,
                       chain: ChainConfig) -> None:
    """Что прямо сейчас разгоняется в наблюдаемых пулах."""
    hunter = ctx.engine.hunter
    ranked = hunter.trending.get(chain.key, [])
    watched = hunter.watched.get(chain.key, 0)
    last = hunter.last_tick.get(chain.key)

    header = (
        f"🚀 <b>Разгон</b> — {esc(chain.name)}\n"
        f"Под наблюдением пулов: {watched}"
    )
    if last is not None:
        age = int((dt.datetime.now(dt.UTC) - last).total_seconds())
        header += f" · замер {age} c назад"

    if not cfg.momentum_enabled:
        header += "\n\n⚠️ Режим выключен: <code>/set momentum on</code>"

    if not ranked:
        await reply(
            message,
            f"{header}\n\nПока сделок в наблюдаемых пулах нет. "
            "Список пополняется новыми пулами сам; добавить токен вручную: "
            "<code>/watch 0xАдрес</code>.",
        )
        return

    lines = [header, ""]
    for index, (signal, _pool, token) in enumerate(ranked[:10], start=1):
        verdict = "✅ проходит" if signal.passed else esc(signal.reasons[0])
        lines.append(
            f"{index}. <code>{token}</code>\n"
            f"    рост {signal.gain_pct:+.1f}% · покупок {signal.buy_ratio * 100:.0f}% · "
            f"сделок {signal.trades} · оборот {fmt_amount(from_wei(signal.volume_native), 3)} "
            f"{chain.native_symbol}\n"
            f"    рейтинг {signal.score} · {verdict}"
        )
    lines.append(
        "\nПороги входа: <code>/set momgain</code>, <code>/set mombuys</code>, "
        "<code>/set momtrades</code>. Все настройки режима: /config"
    )
    await reply(message, "\n".join(lines))


@router.message(Command("watch"))
async def cmd_watch(message: Message, command: CommandObject, ctx: BotContext,
                    chain: ChainConfig) -> None:
    """Добавляет токен в список наблюдения за разгоном."""
    token = extract_address(command.args or "")
    if not token:
        await reply(
            message,
            "Использование: <code>/watch 0xАдресТокена</code>\n"
            "Добавлю токен в наблюдение: бот будет следить за потоком сделок в его пуле "
            "и купит, когда начнётся движение. Список: /trending",
        )
        return

    status = await reply(message, "👀 Ищу пул токена…")
    from sniperbot.chain.dex_adapter import find_best_venue

    client = ctx.registry.get(chain.key)
    venue = await find_best_venue(client, token)
    if venue is None:
        await status.edit_text(
            "❌ Пул с ликвидностью не найден — наблюдать не за чем.", parse_mode="HTML"
        )
        return
    adapter, pool, state = venue

    async with session_scope() as session:
        existing = await repo.watched_pool(session, chain.key, token)
        if existing is not None:
            existing.status = "watch"
            existing.reason = "добавлен вручную"
            pair_address = existing.pair_address
        else:
            record = await repo.add_seen_pair(
                session,
                chain=chain.key,
                pair_address=pool.address,
                token_address=token,
                router_address=adapter.cfg.router,
                dex_kind=pool.kind,
                pool_fee=pool.fee,
                block_number=0,
                status="watch",
                reason="добавлен вручную",
            )
            pair_address = record.pair_address

    await status.edit_text(
        f"👀 <b>Наблюдаю</b> за <code>{token}</code>\n"
        f"Площадка: {esc(adapter.name)} ({pool.label})\n"
        f"Пул: <code>{pair_address}</code>\n"
        f"Ликвидность: {fmt_amount(state.liquidity_native, 4)} {chain.native_symbol}\n\n"
        "Куплю, когда в пуле начнётся движение по вашим порогам. "
        "Проверить: /trending",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
