# Установка и настройка: пошагово

Здесь разобрано **каждый ключ**: что это, где взять и куда вписать. Плюс запуск
из терминала и установка на сервер, чтобы бот работал круглосуточно.

- [1. Что понадобится](#1-что-понадобится)
- [2. Ключи: где взять и куда вписать](#2-ключи-где-взять-и-куда-вписать)
- [3. Полная таблица переменных](#3-полная-таблица-переменных)
- [4. Пример готового .env](#4-пример-готового-env)
- [5. Запуск из терминала](#5-запуск-из-терминала)
- [6. Установка на сервер](#6-установка-на-сервер)
- [7. Обновление, бэкап, перенос](#7-обновление-бэкап-перенос)
- [8. Если что-то не работает](#8-если-что-то-не-работает)

---

## 1. Что понадобится

| Что | Зачем | Стоимость |
|---|---|---|
| Telegram-аккаунт | создать бота у @BotFather | бесплатно |
| Python 3.11+ | запуск | бесплатно |
| VPS (Ubuntu 22.04/24.04, 1 vCPU / 1 ГБ) | бот работает 24/7 | от ~3–5 $/мес |
| RPC-эндпоинт | связь с блокчейном | есть бесплатные тарифы |
| BNB на кошельке бота | покупки и газ | ваши деньги |

Локально бот тоже работает — просто он торгует только пока включён компьютер.

---

## 2. Ключи: где взять и куда вписать

Все ключи живут в одном файле `.env` рядом с проектом (на сервере —
`/opt/memecoin-sniper/.env`). Формат простой: `ИМЯ=значение`, без кавычек и
пробелов вокруг `=`.

Файл можно создать автоматически — команда задаст вопросы и сама сгенерирует
шифровальный ключ:

```bash
sniper init
```

Ниже — что означает каждый ответ, если хотите заполнить вручную.

### 2.1 `BOT_TOKEN` — токен Telegram-бота (обязательно)

1. Откройте в Telegram [@BotFather](https://t.me/BotFather).
2. Отправьте `/newbot`.
3. Введите имя бота (любое, например `My Sniper`).
4. Введите username — должен заканчиваться на `bot`, например `my_meme_sniper_bot`.
5. BotFather пришлёт строку вида `7123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.

```env
BOT_TOKEN=7123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> Токен = полный доступ к боту. Если он утёк — `/revoke` у BotFather и вписать новый.

### 2.2 `MASTER_KEY` — ключ шифрования кошельков (обязательно)

Этим ключом шифруются приватные ключи кошельков всех пользователей. Придумывать
самому не надо — сгенерируйте:

```bash
sniper keygen
# или без установки: python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

```env
MASTER_KEY=m6ESZMoZdf7Qw...jsfcGB
```

> 🔐 **Главное правило проекта.** Потеряете `MASTER_KEY` — база кошельков
> превратится в бесполезный файл, деньги останутся в блокчейне навсегда.
> Сохраните ключ в менеджере паролей **до** первого запуска.
> Менять его на работающем боте нельзя: старые кошельки перестанут открываться.

### 2.3 `ADMIN_IDS` — ваш Telegram ID (желательно)

1. Напишите [@userinfobot](https://t.me/userinfobot) — он ответит числом вида `123456789`.
2. Несколько администраторов — через запятую.

```env
ADMIN_IDS=123456789
```

Даёт команду `/stats` (статистика, состояние RPC).

### 2.4 `ALLOWED_USER_IDS` — белый список (желательно)

Если бот только для вас — впишите те же ID. Пустое значение = ботом может
пользоваться кто угодно, кто найдёт его в поиске (кошельки у всех разные, но
чужие люди будут нагружать ваши RPC).

```env
ALLOWED_USER_IDS=123456789
```

### 2.5 `BSC_RPC_URLS` — ноды BNB Smart Chain (очень желательно)

RPC — это «телефон» блокчейна: через него бот видит новые пары и шлёт сделки.
Публичные ноды из коробки работают, но медленные и часто режут лимиты, а главное —
многие не поддерживают `eth_call` со `state override`, без которого **не работает
проверка на honeypot и налоги**.

Где взять свой (у всех есть бесплатный тариф): **QuickNode**, **NodeReal**,
**Chainstack**, **Ankr**, **GetBlock**, **BlastAPI**. Зарегистрируйтесь → создайте
эндпоинт для BNB Smart Chain Mainnet → скопируйте HTTPS-ссылку.

Указывайте несколько через запятую — бот сам переключится, если одна нода
отвалится (403, лимит, таймаут):

```env
BSC_RPC_URLS=https://ваш-эндпоинт.quiknode.pro/КЛЮЧ/,https://bsc-dataseed.bnbchain.org
```

Проверить, что нода годится, можно командой `sniper doctor` — она пишет,
поддерживается ли `state override`.

### 2.6 `RH_*` — Robinhood Chain

Robinhood Chain — L2 на Arbitrum Orbit, mainnet работает с 1 июля 2026.
Известные параметры сети уже прописаны в `config/chains.json`:

| Параметр | Значение |
|---|---|
| chain_id | `4663` (testnet — `46630`) |
| RPC | `https://rpc.mainnet.chain.robinhood.com` |
| Обозреватель | `https://robinhoodchain.blockscout.com` |
| Газ | ETH |

Не хватает адресов контрактов DEX. Бот умеет работать и с **Uniswap V2**, и с
**Uniswap V3** — можно настроить обе площадки сразу, тогда сканер слушает обе, а
при ручной покупке выбирается пул с наибольшей ликвидностью.

Что нужно для каждой версии:

| Версия | Переменные | Где взять |
|---|---|---|
| V2 | `RH_ROUTER`, `RH_FACTORY`, `RH_WRAPPED_NATIVE` | Router02 и Factory форка Uniswap V2 |
| V3 | `RH_V3_ROUTER`, `RH_V3_FACTORY`, `RH_V3_QUOTER`, `RH_WRAPPED_NATIVE` | SwapRouter, V3 Factory и **QuoterV2** |

Источники адресов:

1. [`docs.robinhood.com/chain/contracts`](https://docs.robinhood.com/chain/contracts) — официальный список контрактов сети (там же адрес WETH);
2. [`developers.uniswap.org/docs/protocols/v2/deployments`](https://developers.uniswap.org/docs/protocols/v2/deployments) и раздел v3 того же сайта — адреса по сетям;
3. обозреватель: откройте любой своп на нужном DEX — контракт, которому шёл вызов, и есть роутер.

Достаточно найти **адрес роутера** — остальное бот достанет сам и заодно определит версию:

```bash
cd /opt/memecoin-sniper
sudo -u sniper .venv/bin/sniper --env-file .env discover 0xАдресРоутера --chain robinhood
```

Команда напечатает готовые строки для `.env`. Для V3 останется добавить только
`RH_V3_QUOTER` — адрес QuoterV2: без него котировки V3 недоступны, и площадка не
включится. Вывести его из роутера нельзя, поэтому ищут его так:

1. **Документация DEX**, раздел Deployments — там QuoterV2 стоит рядом с роутером.
2. **Обозреватель, поиск по верифицированным контрактам**: на Blockscout откройте
   `/verified-contracts` и введите `Quoter` — у Uniswap-развёртываний контракт
   так и называется (`QuoterV2`).
3. **Через создателя фабрики** — самый надёжный путь: откройте адрес V3-фабрики в
   обозревателе, посмотрите, кто её создал (поле Creator), откройте этот адрес и
   пролистайте его транзакции создания контрактов. Весь набор Uniswap (SwapRouter,
   QuoterV2, NonfungiblePositionManager) обычно разворачивает один и тот же адрес.

Найденного кандидата не нужно принимать на веру — проверьте настоящей котировкой:

```bash
sudo -u sniper .venv/bin/sniper --env-file .env discover 0xРоутер --chain robinhood \
     --quoter 0xКандидатВQuoter --token 0xЛюбойТоргуемыйТокен
```

Команда найдёт пул, запросит котировку и скажет прямо: «Котировка работает» —
значит адреса верные, можно вписывать в `.env`. Если Quoter неверный, вы увидите
«пул найден, но котировка не получена».

Дополнительные настройки V3 (нужны редко):

```env
RH_V3_FEES=100,500,2500,10000   # тиры комиссий, если у форка свои
RH_V3_VARIANT=router02          # router01 — если у DEX старый SwapRouter с deadline
RH_DEFAULT_DEX=v3               # какую площадку считать основной
```

Вариант роутера бот определяет сам при первой симуляции, так что трогать
`RH_V3_VARIANT` обычно не нужно.

> ⚠️ Uniswap **v4** и Universal Router не поддерживаются — только V2 и V3.
> Проверить, видит ли бот ликвидность конкретного токена:
> `sniper check 0xАдресТокена --chain robinhood` — в отчёте будет строка
> «Площадка», например `Uniswap V3 · V3 0.3%`.

Testnet-адреса (chain_id 46630) для торговли не годятся — там нет реальной
ликвидности.

Точно так же добавляется любая другая EVM-сеть — блоком в `config/chains.json`.

### 2.7 Комиссия сервиса (если запускаете бота для других)

```env
SERVICE_FEE_BPS=100                  # 100 = 1% от суммы покупки, 0 = выключено
SERVICE_FEE_WALLET=0xВашКошелёк      # куда переводить комиссию
```

Комиссия удерживается из суммы покупки и отправляется отдельной транзакцией.
Максимум — 500 (5%), больше бот не примет.

---

## 3. Полная таблица переменных

| Переменная | Обяз. | По умолчанию | Описание |
|---|:---:|---|---|
| `BOT_TOKEN` | ✅ | — | токен от @BotFather |
| `MASTER_KEY` | ✅ | — | ключ шифрования кошельков (`sniper keygen`) |
| `ADMIN_IDS` | — | пусто | ID администраторов через запятую |
| `ALLOWED_USER_IDS` | — | пусто | белый список; пусто = доступ всем |
| `DATABASE_URL` | — | `sqlite+aiosqlite:///data/sniper.db` | где хранить кошельки и позиции. Относительный путь считается от каталога с `.env`, поэтому база не зависит от того, откуда запущена команда |
| `LOG_LEVEL` | — | `INFO` | `DEBUG` для отладки |
| `ENABLED_CHAINS` | — | из `chains.json` | какие сети включить: `bsc`, `bsc,robinhood` |
| `DEFAULT_CHAIN` | — | `bsc` | сеть по умолчанию для новых пользователей |
| `BSC_RPC_URLS` | — | публичные | свои ноды BSC через запятую |
| `RH_ENABLED` / `RH_CHAIN_ID` / `RH_RPC_URLS` / `RH_ROUTER` / `RH_FACTORY` / `RH_WRAPPED_NATIVE` / `RH_EXPLORER_URL` / `RH_NATIVE_SYMBOL` | — | пусто | параметры Robinhood Chain (V2) |
| `<СЕТЬ>_V3_ROUTER` / `_V3_FACTORY` / `_V3_QUOTER` / `_V3_FEES` / `_V3_VARIANT` | — | пусто | Uniswap V3 в этой сети (например `BSC_V3_ROUTER`, `RH_V3_QUOTER`) |
| `<СЕТЬ>_DEFAULT_DEX` | — | `v2` | какая площадка основная: `v2` или `v3` |
| `SCANNER_POLL_INTERVAL` | — | `2.0` | период опроса новых пар, сек |
| `SCANNER_LIQUIDITY_WAIT_BLOCKS` | — | `60` | сколько ждать залив ликвидности |
| `POSITION_POLL_INTERVAL` | — | `6.0` | период пересчёта позиций (TP/SL), сек |
| `DEPOSIT_POLL_INTERVAL` | — | `30.0` | период проверки пополнений, сек |
| `SERVICE_FEE_BPS` / `SERVICE_FEE_WALLET` | — | `0` | комиссия сервиса |

Переменные окружения перекрывают `.env` — удобно для Docker и systemd.

---

## 4. Пример готового `.env`

```env
# --- Telegram ---
BOT_TOKEN=7123456789:AAF-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ADMIN_IDS=123456789
ALLOWED_USER_IDS=123456789

# --- Шифрование (НЕ ТЕРЯТЬ!) ---
MASTER_KEY=m6ESZMoZdf7QwLpVn2Kx8Bt4YrHc1JsfcGBaQwErTyUiOp

# --- Хранилище ---
DATABASE_URL=sqlite+aiosqlite:///data/sniper.db
LOG_LEVEL=INFO

# --- Сети ---
ENABLED_CHAINS=bsc
DEFAULT_CHAIN=bsc
BSC_RPC_URLS=https://bsc.ваш-провайдер.io/КЛЮЧ,https://bsc-dataseed.bnbchain.org

# --- Robinhood Chain (пока выключена) ---
RH_ENABLED=false

# --- Движок ---
SCANNER_POLL_INTERVAL=2.0
POSITION_POLL_INTERVAL=6.0
DEPOSIT_POLL_INTERVAL=30.0

# --- Комиссия сервиса ---
SERVICE_FEE_BPS=0
```

---

## 5. Запуск из терминала

```bash
git clone https://github.com/gytfel/mem-robinhood-and-bnb.git
cd mem-robinhood-and-bnb

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # появится команда sniper

sniper init                        # создаст .env и MASTER_KEY
sniper doctor                      # проверит токен, ноды, поддержку симуляции
sniper run                         # запуск (Ctrl+C — остановка)
```

Дальше откройте своего бота в Telegram и отправьте `/start` — он создаст кошелёк
и покажет адрес для пополнения.

### Команды CLI

| Команда | Что делает |
|---|---|
| `sniper init` | создаёт `.env`, генерирует `MASTER_KEY` (`--force` — перезаписать) |
| `sniper doctor` | проверяет ключи, Telegram, каждую ноду, поддержку `state override` |
| `sniper run` | запускает бота (`--log-level DEBUG` для подробных логов) |
| `sniper check 0xТокен` | полная проверка токена прямо в терминале, без Telegram |
| `sniper wallets` | список пользователей, их адреса и балансы |
| `sniper keygen` | печатает новый `MASTER_KEY` |

У всех команд есть общий флаг `--env-file /путь/к/.env`.

```bash
# пример: проверить токен перед покупкой
sniper check 0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82 --chain bsc --amount 0.05
```

---

## 6. Установка на сервер

### Шаг 1. Купить VPS

Подойдёт любой: Hetzner, Timeweb, Aeza, Contabo, DigitalOcean. Минимум —
1 vCPU, 1 ГБ RAM, Ubuntu 22.04 или 24.04. Ближе к ноде — быстрее снайп
(для BSC обычно хороши Германия/Нидерланды/Сингапур).

### Шаг 2. Подключиться по SSH

```bash
ssh root@ВАШ_IP        # пароль или ключ выдаёт хостер
```

### Шаг 3. Скопировать проект на сервер

```bash
apt update && apt install -y git
git clone https://github.com/gytfel/mem-robinhood-and-bnb.git
cd mem-robinhood-and-bnb

# проверьте, что скрипт установки на месте:
ls scripts/install-server.sh
```

Если файла нет — значит свежая версия ещё не влита в `main`. Переключитесь на
рабочую ветку:

```bash
git checkout claude/memecoin-sniper-bot-1nkbtm
```

### Шаг 4. Установить одной командой

```bash
sudo bash scripts/install-server.sh
```

Скрипт сам: поставит Python и зависимости, создаст системного пользователя
`sniper`, установит бота в `/opt/memecoin-sniper`, спросит `BOT_TOKEN` и ваш
Telegram ID, сгенерирует `MASTER_KEY`, зарегистрирует systemd-сервис,
прогонит диагностику и запустит бота.

Без вопросов (например, из своего скрипта):

```bash
sudo BOT_TOKEN=7123456789:AAF-xxx ADMIN_IDS=123456789 \
     BSC_RPC_URLS=https://bsc.провайдер/КЛЮЧ \
     bash scripts/install-server.sh
```

### Шаг 5. Управление сервисом

```bash
systemctl status memecoin-sniper        # состояние
journalctl -u memecoin-sniper -f        # логи в реальном времени
systemctl restart memecoin-sniper       # перезапуск (после правки .env)
systemctl stop memecoin-sniper          # остановить
systemctl disable memecoin-sniper       # убрать из автозапуска
```

Бот запускается автоматически при перезагрузке сервера и перезапускается сам,
если упал.

### Правка ключей на сервере

```bash
sudo nano /opt/memecoin-sniper/.env
sudo systemctl restart memecoin-sniper
```

### Вариант через Docker

```bash
cp .env.example .env && nano .env       # вписать BOT_TOKEN и MASTER_KEY
docker compose up -d --build
docker compose logs -f
```

---

## 7. Обновление, бэкап, перенос

```bash
cd ~/mem-robinhood-and-bnb && git pull
sudo bash scripts/update.sh             # копирует код, обновляет зависимости, рестартит

bash scripts/backup.sh                  # копия базы в /opt/memecoin-sniper/backups
```

Регулярный бэкап (каждый день в 4:00):

```bash
sudo crontab -e
# добавить строку:
0 4 * * * bash /opt/memecoin-sniper/scripts/backup.sh >/dev/null 2>&1
```

**Перенос на другой сервер** — нужны ровно два предмета:

1. `/opt/memecoin-sniper/.env` (в нём `MASTER_KEY`);
2. `/opt/memecoin-sniper/data/sniper.db`.

Установите бота на новом сервере, положите оба файла на место, выполните
`systemctl restart memecoin-sniper` — все кошельки и позиции на месте.
**Не запускайте два экземпляра с одним `BOT_TOKEN` одновременно** — Telegram
разорвёт соединение обоим.

---

## 8. Если что-то не работает

Первым делом: `sniper doctor` (на сервере —
`sudo -u sniper /opt/memecoin-sniper/.venv/bin/sniper --env-file /opt/memecoin-sniper/.env doctor`).

| Симптом | Причина и лечение |
|---|---|
| `Telegram не принял BOT_TOKEN (401)` | токен неверный или отозван — возьмите новый у @BotFather |
| `Conflict: terminated by other getUpdates` | бот запущен дважды (например, локально и на сервере) — остановите лишний |
| `RPC недоступен` / 403 | нода режет доступ; добавьте свои эндпоинты в `BSC_RPC_URLS` |
| `RPC без state override` | нода не умеет симуляцию: honeypot и налоги не проверить. Смените провайдера либо отключите «Требовать симуляцию» в ⚙️ настройках (опаснее) |
| `Недостаточно BNB` при покупке | на кошельке нет денег на сумму + газ; пополните адрес из 💼 «Кошелёк» |
| Транзакция «revert» | высокий налог, лимит на покупку или торговля ещё закрыта — поднимите проскальзывание или пропустите токен |
| `MASTER_KEY ... неверный` | `.env` заменили после создания кошельков — верните прежний `MASTER_KEY` |
| `каталог data недоступен на запись` | версия до этого исправления считала путь от текущего каталога. Обновитесь: `sudo bash scripts/update.sh` |
| Бот молчит | `journalctl -u memecoin-sniper -n 100` покажет причину |
| Не приходят уведомления о пополнении | первый замер баланса не считается пополнением; проверьте, что сеть включена в `ENABLED_CHAINS` |

**Безопасность напоследок**

- `.env` держите с правами `600` (скрипт установки делает это сам);
- бэкапьте `MASTER_KEY` отдельно от базы;
- на кошельке бота держите только те средства, что готовы потерять;
- никогда не пересылайте свой приватный ключ третьим лицам — ни один настоящий
  админ его не попросит.
