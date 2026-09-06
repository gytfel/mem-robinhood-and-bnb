"""Командная строка бота.

    sniper init      — создать .env и ключи
    sniper doctor    — проверить конфигурацию, Telegram и RPC
    sniper run       — запустить бота
    sniper check     — проверить токен прямо из терминала
    sniper wallets   — список кошельков пользователей
    sniper keygen    — сгенерировать MASTER_KEY

Модуль намеренно импортирует внутренности лениво: файл .env выбирается
флагом --env-file, а конфигурация читает его в момент импорта.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent

OK = "✅"
BAD = "❌"
WARN = "⚠️ "
SKIP = "⏭ "

BOT_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


# --------------------------------------------------------------------------- утилиты
def die(message: str, code: int = 1) -> None:
    print(f"{BAD} {message}", file=sys.stderr)
    raise SystemExit(code)


def generate_master_key() -> str:
    return secrets.token_urlsafe(48)


def mask(value: str, head: int = 8, tail: int = 4) -> str:
    if not value:
        return "—"
    if len(value) <= head + tail:
        return value[:2] + "…"
    return f"{value[:head]}…{value[-tail:]}"


def set_env_line(text: str, key: str, value: str) -> str:
    """Меняет значение KEY=... в тексте .env, сохраняя комментарии."""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(f"{key}={value}", text, count=1)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def env_template() -> str:
    example = ROOT / ".env.example"
    if example.exists():
        return example.read_text(encoding="utf-8")
    return (
        "BOT_TOKEN=\nADMIN_IDS=\nALLOWED_USER_IDS=\nMASTER_KEY=\n"
        "DATABASE_URL=sqlite+aiosqlite:///data/sniper.db\nLOG_LEVEL=INFO\n"
        "ENABLED_CHAINS=bsc\nDEFAULT_CHAIN=bsc\nBSC_RPC_URLS=\n"
    )


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{mask(default) if secret else default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(130) from None
    return answer or default


# ------------------------------------------------------------------------------ init
def cmd_init(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file).resolve()
    if env_path.exists() and not args.force:
        die(f"Файл {env_path} уже существует. Используйте --force, чтобы перезаписать.")

    interactive = not args.yes
    print(f"\n🔧 Настройка Memecoin Sniper → {env_path}\n")

    bot_token = args.bot_token or ""
    if interactive and not bot_token:
        print("1) Токен бота. Откройте @BotFather в Telegram → /newbot → скопируйте токен.")
        bot_token = ask("   BOT_TOKEN")
    if not bot_token:
        die("BOT_TOKEN обязателен (--bot-token или ответ на вопрос)")
    if not BOT_TOKEN_RE.match(bot_token):
        print(f"{WARN}Токен не похож на формат 123456789:AA... — сохраняю как есть.")

    admin_ids = args.admin_id or ""
    if interactive and not admin_ids:
        print("\n2) Ваш Telegram ID. Узнать: напишите @userinfobot.")
        admin_ids = ask("   ADMIN_IDS (через запятую, можно пропустить)")

    allowed = ""
    if admin_ids:
        if args.private is not None:
            private = args.private
        elif interactive:
            private = ask("\n3) Закрыть бота только для этих ID? (y/n)", "y").lower().startswith("y")
        else:
            private = False
        if private:
            allowed = admin_ids

    rpc = args.bsc_rpc or ""
    if interactive and not rpc:
        print("\n4) Свой RPC для BNB Smart Chain ускоряет снайп (Enter — публичные ноды).")
        rpc = ask("   BSC_RPC_URLS (через запятую)")

    master_key = args.master_key or generate_master_key()

    text = env_template()
    for key, value in (
        ("BOT_TOKEN", bot_token),
        ("ADMIN_IDS", admin_ids),
        ("ALLOWED_USER_IDS", allowed),
        ("MASTER_KEY", master_key),
        ("BSC_RPC_URLS", rpc),
    ):
        text = set_env_line(text, key, value)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(text, encoding="utf-8")
    os.chmod(env_path, 0o600)

    print(f"\n{OK} Файл {env_path} создан (права 600 — читает только владелец).")
    print(f"{OK} MASTER_KEY сгенерирован: {mask(master_key, 10, 6)}")
    print(
        "\n🔐 СОХРАНИТЕ MASTER_KEY В НАДЁЖНОМ МЕСТЕ.\n"
        "   Им зашифрованы приватные ключи кошельков: потеряете ключ — потеряете доступ\n"
        "   к деньгам пользователей, даже имея базу данных.\n"
    )
    print("Дальше:  sniper doctor   — проверить связь с Telegram и сетями")
    print("         sniper run      — запустить бота\n")
    return 0


# ---------------------------------------------------------------------------- keygen
def cmd_keygen(_: argparse.Namespace) -> int:
    print(generate_master_key())
    return 0


# ---------------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_doctor(args))


async def _doctor(args: argparse.Namespace) -> int:
    import time

    import aiohttp

    from sniperbot.chain.clients import ChainClient
    from sniperbot.config import env_prefix, get_settings, load_chains
    from sniperbot.security.keyvault import KeyVault, VaultError
    from sniperbot.sniper.safety import HoneypotSimulator

    problems = 0
    env_path = Path(args.env_file).resolve()
    print("\n🔎 Диагностика Memecoin Sniper\n")

    print("Конфигурация")
    if env_path.exists():
        mode = oct(env_path.stat().st_mode & 0o777)[2:]
        print(f"  {OK} .env найден: {env_path} (права {mode})")
        if mode not in {"600", "400"}:
            print(f"  {WARN}Рекомендуется chmod 600 {env_path}")
    else:
        print(f"  {BAD} .env не найден: {env_path} — выполните `sniper init`")
        problems += 1

    settings = get_settings()
    for issue in settings.validate_runtime():
        print(f"  {BAD} {issue}")
        problems += 1

    if settings.bot_token:
        print(f"  {OK} BOT_TOKEN задан ({mask(settings.bot_token)})")
    if len(settings.master_key) >= 16:
        try:
            fingerprint = KeyVault(settings.master_key).fingerprint()
            print(f"  {OK} MASTER_KEY задан ({len(settings.master_key)} симв., отпечаток {fingerprint})")
        except VaultError as exc:
            print(f"  {BAD} MASTER_KEY: {exc}")
            problems += 1

    database_url = settings.resolved_database_url
    db_ok, db_note = _check_database(database_url)
    print(f"  {OK if db_ok else BAD} База данных: {database_url} — {db_note}")
    problems += 0 if db_ok else 1

    if settings.allowed_user_ids:
        print(f"  {OK} Доступ ограничен: {len(settings.allowed_user_ids)} польз.")
    else:
        print(f"  {WARN}ALLOWED_USER_IDS пуст — ботом сможет пользоваться кто угодно")

    # ------------------------------------------------------------------ Telegram
    print("\nTelegram")
    if not settings.bot_token:
        print(f"  {SKIP} пропускаю — нет BOT_TOKEN")
    else:
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"https://api.telegram.org/bot{settings.bot_token}/getMe"
                async with session.get(url) as response:
                    status = response.status
                    body = await response.text()
            data = _json_or_empty(body)
            if data.get("ok"):
                me = data["result"]
                print(f"  {OK} Бот @{me.get('username')} (id {me.get('id')})")
                problems += 0
            elif status in (401, 404):
                print(f"  {BAD} Telegram не принял BOT_TOKEN ({status}): токен неверный или отозван")
                problems += 1
            else:
                detail = data.get("description") or body[:120]
                print(f"  {BAD} Telegram ответил {status}: {detail}")
                problems += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {BAD} Не смог связаться с api.telegram.org: {str(exc)[:160]}")
            print("      Проверьте интернет на сервере и что домен не заблокирован.")
            problems += 1

    # ---------------------------------------------------------------------- сети
    print("\nСети")
    chains = load_chains(settings=settings)
    clients: list = []
    ready = 0
    for key, config in chains.items():
        print(f"  {config.name} ({key})")
        if not config.configured:
            prefix = env_prefix(key)
            gaps = ", ".join(f"{prefix}_{name}" for name in config.missing)
            print(f"    {SKIP} не заполнено в .env: {gaps}")
            continue
        if not config.enabled:
            print(f"    {SKIP} выключена: добавьте «{key}» в ENABLED_CHAINS")
            continue

        try:
            client = ChainClient(config)
            clients.append(client)
            started = time.perf_counter()
            block = await client.block_number()
            latency = (time.perf_counter() - started) * 1000
            print(f"    {OK} RPC {client.rpc_url} — блок {block}, {latency:.0f} мс")
        except Exception as exc:  # noqa: BLE001
            print(f"    {BAD} RPC недоступен: {str(exc)[:160]}")
            problems += 1
            continue

        try:
            chain_id = await client.run(lambda w3: w3.eth.chain_id)
            if chain_id == config.chain_id:
                print(f"    {OK} chain_id {chain_id} совпадает с конфигом")
            else:
                print(f"    {BAD} chain_id ноды {chain_id} ≠ {config.chain_id} в конфиге")
                problems += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    {WARN}не смог прочитать chain_id: {exc}")

        for router_cfg in config.active_routers:
            await _check_router(client, config, router_cfg, print)

        try:
            from sniperbot.chain.dex_adapter import PoolRef, get_adapter

            adapter = get_adapter(client, config.default_router)
            supported = await HoneypotSimulator(client, adapter, PoolRef("0x")).supports_override()
        except Exception:  # noqa: BLE001
            supported = False
        if supported:
            print(f"    {OK} state override поддерживается — симуляция honeypot и налогов работает")
        else:
            print(f"    {WARN}RPC без state override: honeypot/налоги проверить нельзя.")
            print("       Возьмите ноду с поддержкой eth_call+stateOverride или выключите")
            print("       «Требовать симуляцию» в настройках бота (это опаснее).")
        ready += 1

    for client in clients:
        await client.close()

    print(f"\nИтог: сетей готово к торговле — {ready}, проблем — {problems}")
    if problems:
        print("Исправьте пункты с ❌ и запустите `sniper doctor` ещё раз.\n")
        return 1
    print("Можно запускать: sniper run\n")
    return 0


async def _check_router(client, config, router_cfg, out) -> None:  # noqa: ANN001
    """Сверяет адреса роутера с конфигом: WETH и фабрика должны совпадать."""
    from sniperbot.chain.abi import ROUTER_ABI, V3_ROUTER02_ABI
    from sniperbot.utils.evm import has_code

    label = f"{router_cfg.name} [{router_cfg.kind}]"
    try:
        if router_cfg.is_v3:
            weth = await client.call(router_cfg.router, V3_ROUTER02_ABI, "WETH9")
            factory = await client.call(router_cfg.router, V3_ROUTER02_ABI, "factory")
        else:
            weth = await client.call(router_cfg.router, ROUTER_ABI, "WETH")
            factory = await client.call(router_cfg.router, ROUTER_ABI, "factory")
    except Exception as exc:  # noqa: BLE001
        out(f"    {BAD} {label}: роутер не отвечает — {str(exc)[:120]}")
        return

    if weth.lower() != config.wrapped_native.lower():
        out(f"    {BAD} {label}: WETH={weth}, а в конфиге {config.wrapped_native}")
        return
    if factory.lower() != router_cfg.factory.lower():
        out(f"    {BAD} {label}: factory={factory}, а в конфиге {router_cfg.factory}")
        return
    out(f"    {OK} {label}: роутер и фабрика совпадают с конфигом")

    if router_cfg.is_v3:
        try:
            quoter_ok = has_code(await client.run(lambda w3: w3.eth.get_code(router_cfg.quoter)))
        except Exception:  # noqa: BLE001
            quoter_ok = False
        if quoter_ok:
            tiers = ", ".join(f"{f / 10_000:g}%" for f in router_cfg.fee_tiers)
            out(f"    {OK} {label}: Quoter на месте, тиры {tiers}")
        else:
            out(f"    {BAD} {label}: по адресу Quoter нет контракта — котировки V3 работать не будут")


def _json_or_empty(body: str) -> dict:
    import json

    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _check_database(url: str) -> tuple[bool, str]:
    marker = "sqlite+aiosqlite:///"
    if not url.startswith(marker):
        return True, "внешняя СУБД, проверяется при запуске"
    path = Path(url[len(marker) :])
    if str(path) == ":memory:":
        return True, "в памяти (данные не сохраняются!)"
    directory = path.parent if path.parent != Path("") else Path(".")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"каталог {directory} недоступен на запись ({exc})"
    exists = "файл есть" if path.exists() else "будет создана при первом запуске"
    return True, f"каталог {directory} доступен на запись, {exists}"


# ------------------------------------------------------------------------------- run
def cmd_run(args: argparse.Namespace) -> int:
    import asyncio

    from sniperbot.bot.app import run_bot
    from sniperbot.config import get_settings
    from sniperbot.logging_setup import setup_logging

    settings = get_settings()
    setup_logging(args.log_level or settings.log_level)
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем")
    return 0


# ----------------------------------------------------------------------------- check
def cmd_check(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_check(args))


async def _check(args: argparse.Namespace) -> int:
    from decimal import Decimal

    from sniperbot.chain.clients import ChainClient
    from sniperbot.config import env_prefix, get_settings, load_chains
    from sniperbot.sniper.safety import analyze_best
    from sniperbot.utils.evm import extract_address
    from sniperbot.utils.fmt import to_wei

    token = extract_address(args.token)
    if not token:
        die(f"«{args.token}» не похоже на адрес токена (нужен формат 0x…)")

    settings = get_settings()
    chains = load_chains(settings=settings)
    key = args.chain or settings.default_chain
    config = chains.get(key)
    if config is None:
        die(f"Сеть {key} не описана в config/chains.json")
    if not config.configured:
        prefix = env_prefix(key)
        gaps = ", ".join(f"{prefix}_{name}" for name in config.missing)
        die(f"Сеть {config.name} не настроена — заполните в .env: {gaps}")

    client = ChainClient(config)
    amount = Decimal(str(args.amount))
    print(f"\n🔎 Проверяю {token} в сети {config.name} (сумма симуляции {amount} {config.native_symbol})…\n")

    try:
        report = await analyze_best(
            client, token,
            amount_native_wei=to_wei(amount, config.native_decimals),
            settings=None, run_simulation=not args.no_simulation,
        )
    except Exception as exc:  # noqa: BLE001 - в терминале нужен внятный текст, а не трейсбек
        await client.close()
        die(f"Не удалось проверить токен: {str(exc)[:200]}\n"
            f"   Проверьте доступность RPC: sniper doctor")
        return 1

    token_info = report.token
    print(f"Токен:        {token_info.symbol} — {token_info.name}")
    print(f"Адрес:        {token_info.address}")
    print(f"Decimals:     {token_info.decimals}")
    print(f"Владелец:     {'renounced' if token_info.renounced else (token_info.owner or 'неизвестен')}")
    if report.venue:
        print(f"Площадка:     {report.venue}")
    if report.pair:
        print(f"Пул:          {report.pair}")
        print(f"Ликвидность:  {report.liquidity_native:.4f} {config.native_symbol}")
    if report.lp_burned is not None:
        print(f"LP сожжён:    {report.lp_burned:.1f}%")
    simulation = report.simulation
    if simulation.available:
        buy = "—" if report.buy_tax_pct is None else f"{report.buy_tax_pct:.1f}%"
        sell = "—" if report.sell_tax_pct is None else f"{report.sell_tax_pct:.1f}%"
        print(f"Налоги:       покупка {buy} / продажа {sell}")
        print(f"Honeypot:     {'НЕТ' if simulation.can_sell else 'ДА — продать нельзя'}")
    elif simulation.error:
        print(f"Симуляция:    недоступна ({simulation.error})")

    print("\nПроверки:")
    for check in report.checks:
        detail = f" — {check.detail}" if check.detail else ""
        print(f"  {check.icon} {check.title}{detail}")

    verdict = {"safe": "🟢 явных проблем не найдено",
               "risky": "🟠 есть риски",
               "danger": "🔴 опасно"}[report.verdict]
    print(f"\nИтог: {verdict} ({report.score}/100)")
    if report.blocking:
        print("Блокирующие проблемы: " + ", ".join(c.title for c in report.blocking))
    print(f"Обозреватель: {config.token_url(token)}\n")
    await client.close()
    return 0 if report.verdict != "danger" else 2


# -------------------------------------------------------------------------- discover
async def probe_router(client, router: str) -> dict:
    """Определяет версию роутера и достаёт у него фабрику и WETH.

    V2 отвечает на `WETH()`, V3 — на `WETH9()`; фабрика проверяется вызовом,
    который есть только у неё (`allPairsLength` у V2, `getPool` у V3).
    """
    from sniperbot.chain.abi import FACTORY_ABI, ROUTER_ABI, V3_FACTORY_ABI, V3_ROUTER02_ABI
    from sniperbot.utils.evm import has_code

    if not has_code(await client.run(lambda w3: w3.eth.get_code(router))):
        raise ValueError("по этому адресу нет кода — это не контракт")

    try:
        weth = await client.call(router, ROUTER_ABI, "WETH")
        factory = await client.call(router, ROUTER_ABI, "factory")
        pairs = int(await client.call(factory, FACTORY_ABI, "allPairsLength"))
        return {"kind": "v2", "router": router, "weth": weth, "factory": factory, "pairs": pairs}
    except Exception as v2_error:  # noqa: BLE001 - пробуем V3
        try:
            weth = await client.call(router, V3_ROUTER02_ABI, "WETH9")
            factory = await client.call(router, V3_ROUTER02_ABI, "factory")
            # У фабрики V3 есть getPool: вызов не должен реветить (адрес может быть нулевым).
            await client.call(factory, V3_FACTORY_ABI, "getPool", weth, weth, 3000)
            return {"kind": "v3", "router": router, "weth": weth, "factory": factory, "pairs": None}
        except Exception as v3_error:  # noqa: BLE001
            raise ValueError(f"ни V2 ({v2_error}), ни V3 ({v3_error})") from v3_error


def cmd_discover(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_discover(args))


async def _discover(args: argparse.Namespace) -> int:
    from sniperbot.chain.clients import ChainClient
    from sniperbot.config import env_prefix, get_settings, load_chains
    from sniperbot.utils.evm import extract_address

    router = extract_address(args.router)
    if not router:
        die(f"«{args.router}» не похоже на адрес (нужен формат 0x… из 42 символов)")

    settings = get_settings()
    chains = load_chains(settings=settings)
    key = args.chain or settings.default_chain
    config = chains.get(key)
    if config is None:
        die(f"Сеть {key} не описана в config/chains.json")
    if not config.rpc_urls:
        die(f"Для сети {config.name} не задан RPC — заполните {env_prefix(key)}_RPC_URLS в .env")

    print(f"\n🔎 Спрашиваю роутер {router} в сети {config.name}…\n")
    client = ChainClient(config)
    try:
        try:
            found = await probe_router(client, router)
        except Exception as exc:  # noqa: BLE001 - показываем причину, а не трейсбек
            die(f"Не похоже на роутер Uniswap: {str(exc)[:200]}\n"
                "   Universal Router (v4) не поддерживается — нужен V2 Router02 или V3 SwapRouter.")
            return 1

        quote_note = ""
        if args.token:
            quote_note = await _verify_quote(client, found, args)
    finally:
        await client.close()

    prefix = env_prefix(key)
    if found["kind"] == "v2":
        print(f"{OK} Это роутер Uniswap V2: фабрика знает о {found['pairs']} парах\n")
        print("Скопируйте эти строки в .env:\n")
        print(f"{prefix}_ENABLED=true")
        print(f"{prefix}_ROUTER={found['router']}")
        print(f"{prefix}_FACTORY={found['factory']}")
        print(f"{prefix}_WRAPPED_NATIVE={found['weth']}")
    else:
        print(f"{OK} Это роутер Uniswap V3 (SwapRouter)\n")
        print("Скопируйте эти строки в .env:\n")
        print(f"{prefix}_ENABLED=true")
        print(f"{prefix}_V3_ROUTER={found['router']}")
        print(f"{prefix}_V3_FACTORY={found['factory']}")
        print(f"{prefix}_WRAPPED_NATIVE={found['weth']}")
        print(f"{prefix}_V3_QUOTER=0x…            # адрес QuoterV2 из документации DEX")
        print(f"# {prefix}_V3_FEES=100,500,3000,10000   # тиры комиссий, если у форка свои")
        print(f"\n{WARN}Без QuoterV2 котировки для V3 недоступны — найдите его адрес там же,")
        print("   где брали роутер (docs DEX, раздел Deployments).")
    if quote_note:
        print(quote_note)
    elif found["kind"] == "v3" and not args.quoter:
        print(f"\nПроверить кандидата в Quoter:\n"
              f"  sniper discover {router} --chain {key} --quoter 0xКандидат --token 0xТокен")
    print("\nПосле правки .env выполните: sniper doctor\n")
    return 0


async def _verify_quote(client, found: dict, args: argparse.Namespace) -> str:
    """Проверяет связку роутер+фабрика(+Quoter) настоящей котировкой по токену."""
    from decimal import Decimal

    from sniperbot.chain.dex_adapter import get_adapter
    from sniperbot.config import RouterConfig
    from sniperbot.utils.evm import extract_address
    from sniperbot.utils.fmt import fmt_amount, from_wei, to_wei

    token = extract_address(args.token)
    if not token:
        return f"\n{WARN}--token: «{args.token}» не похоже на адрес — котировку не проверял."

    cfg = RouterConfig(
        name="проверка", router=found["router"], factory=found["factory"], default=True,
        kind=found["kind"], quoter=extract_address(args.quoter or "") or "",
    )
    if found["kind"] == "v3" and not cfg.quoter:
        return f"\n{WARN}Для проверки котировки V3 добавьте --quoter 0xКандидат."

    adapter = get_adapter(client, cfg)
    try:
        pool = await adapter.find_pool(token)
    except Exception as exc:  # noqa: BLE001
        return f"\n{BAD} Поиск пула не удался: {str(exc)[:160]}"
    if pool is None:
        return (f"\n{BAD} Пул {token} с нативной монетой не найден на этой площадке.\n"
                "   Возможно, токен торгуется на другой версии протокола или в другой паре.")

    amount = to_wei(Decimal(str(args.amount)), client.config.native_decimals)
    try:
        out = await adapter.quote_buy(token, amount, pool)
    except Exception as exc:  # noqa: BLE001
        return (f"\n{BAD} Пул найден ({pool.label}), но котировка не получена: {str(exc)[:160]}\n"
                "   Для V3 это обычно значит, что адрес Quoter неверный.")

    symbol = client.config.native_symbol
    return (f"\n{OK} Котировка работает: за {args.amount} {symbol} дают "
            f"{fmt_amount(from_wei(out, 18), 4)} токенов (пул {pool.label}, {pool.address}).\n"
            "   Значит адреса верные — можно вписывать в .env.")


# --------------------------------------------------------------------------- wallets
def cmd_wallets(args: argparse.Namespace) -> int:
    import asyncio

    return asyncio.run(_wallets(args))


async def _wallets(args: argparse.Namespace) -> int:
    from sniperbot.chain.clients import ChainClient
    from sniperbot.config import get_settings, load_chains
    from sniperbot.db import repo
    from sniperbot.db.base import close_db, init_db, session_scope
    from sniperbot.utils.fmt import fmt_amount, from_wei

    settings = get_settings()
    await init_db(settings.resolved_database_url)
    clients: dict = {}
    try:
        async with session_scope() as session:
            users = await repo.all_users(session)
            rows = []
            for user in users:
                positions = await repo.open_positions(session, user_id=user.id)
                rows.append((user, len(positions)))

        if not rows:
            print("Пользователей пока нет — кошельки создаются при первом /start.")
            return 0

        chains = {k: c for k, c in load_chains(settings=settings).items() if c.configured and c.enabled}
        if not args.no_balances:
            clients = {k: ChainClient(c) for k, c in chains.items()}

        print(f"\n👛 Кошельки пользователей ({len(rows)})\n")
        for user, open_positions in rows:
            name = f"@{user.username}" if user.username else f"id {user.id}"
            print(f"  {name} · {user.wallet_address}")
            for key, client in clients.items():
                try:
                    balance = await client.native_balance(user.wallet_address)
                    print(f"      {chains[key].name}: {fmt_amount(from_wei(balance))} {chains[key].native_symbol}")
                except Exception as exc:  # noqa: BLE001 - нода могла отвалиться
                    print(f"      {chains[key].name}: баланс недоступен ({str(exc)[:60]})")
            print(f"      открытых позиций: {open_positions}")
        print()
        return 0
    finally:
        for client in clients.values():
            await client.close()
        await close_db()


# ------------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sniper",
        description="Memecoin Sniper Bot — снайпинг мемкоинов в BSC и Robinhood Chain",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  sniper init                     создать .env и ключи\n"
            "  sniper doctor                   проверить конфигурацию и связь\n"
            "  sniper run                      запустить бота\n"
            "  sniper check 0xТокен            проверить токен из терминала\n"
            "  sniper discover 0xРоутер        достать адреса фабрики и WETH для .env\n"
            "  sniper wallets                  кошельки пользователей и балансы\n"
        ),
    )
    parser.add_argument("--env-file", default=os.getenv("SNIPER_ENV_FILE", ".env"),
                        help="путь к .env (по умолчанию ./.env)")
    parser.add_argument("--version", action="version", version=f"Memecoin Sniper Bot {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="создать .env, сгенерировать MASTER_KEY")
    init_parser.add_argument("--bot-token", help="токен от @BotFather")
    init_parser.add_argument("--admin-id", help="Telegram ID администраторов через запятую")
    init_parser.add_argument("--master-key", help="свой MASTER_KEY (по умолчанию генерируется)")
    init_parser.add_argument("--bsc-rpc", help="свои RPC для BSC через запятую")
    init_parser.add_argument("--private", action="store_true", default=None,
                             help="разрешить доступ только администраторам")
    init_parser.add_argument("--force", action="store_true", help="перезаписать существующий .env")
    init_parser.add_argument("-y", "--yes", action="store_true", help="без вопросов (для скриптов)")
    init_parser.set_defaults(func=cmd_init)

    doctor_parser = subparsers.add_parser("doctor", help="проверить конфигурацию, Telegram и RPC")
    doctor_parser.set_defaults(func=cmd_doctor)

    run_parser = subparsers.add_parser("run", help="запустить бота")
    run_parser.add_argument("--log-level", help="DEBUG / INFO / WARNING")
    run_parser.set_defaults(func=cmd_run)

    check_parser = subparsers.add_parser("check", help="проверить токен без покупки")
    check_parser.add_argument("token", help="адрес токена 0x…")
    check_parser.add_argument("--chain", help="ключ сети (bsc, robinhood, …)")
    check_parser.add_argument("--amount", default="0.01", help="сумма для симуляции (по умолчанию 0.01)")
    check_parser.add_argument("--no-simulation", action="store_true", help="без симуляции сделки")
    check_parser.set_defaults(func=cmd_check)

    discover_parser = subparsers.add_parser(
        "discover", help="по адресу роутера DEX найти фабрику и WETH для .env")
    discover_parser.add_argument("router", help="адрес роутера DEX (V2 Router02 или V3 SwapRouter)")
    discover_parser.add_argument("--chain", help="ключ сети (bsc, robinhood, …)")
    discover_parser.add_argument("--quoter", help="проверить кандидата в QuoterV2 (для V3)")
    discover_parser.add_argument("--token", help="токен, на котором проверить котировку")
    discover_parser.add_argument("--amount", default="0.01", help="сумма проверочной котировки")
    discover_parser.set_defaults(func=cmd_discover)

    wallets_parser = subparsers.add_parser("wallets", help="кошельки пользователей и балансы")
    wallets_parser.add_argument("--no-balances", action="store_true", help="не запрашивать балансы")
    wallets_parser.set_defaults(func=cmd_wallets)

    keygen_parser = subparsers.add_parser("keygen", help="сгенерировать MASTER_KEY")
    keygen_parser.set_defaults(func=cmd_keygen)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    # Конфигурация читает .env в момент импорта, поэтому путь выставляем заранее.
    os.environ["SNIPER_ENV_FILE"] = str(Path(args.env_file))
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nПрервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())
