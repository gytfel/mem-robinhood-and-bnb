"""Поток секвенсора Arbitrum Nitro: события раньше, чем их отдаст RPC.

В сетях на Nitro (к ним относится Robinhood Chain) нет общего мемпула. Порядок
сделок определяет один секвенсор и объявляет своё решение в WebSocket. Обычный
RPC-узел, прежде чем ответить на вопрос, обязан переисполнить блок — поэтому он
узнаёт о происходящем позже. Подписка на поток убирает именно это опоздание.

Важное ограничение, из которого следует вся конструкция: поток сообщает о том,
что секвенсор **уже решил и уже исполнил**. Опередить чужую сделку нельзя, и
данные из потока — мягкое подтверждение: транзакция ещё может провалиться или
быть отфильтрована. Поэтому поток здесь используется только как звонок
будильника: «посмотри сейчас». Читать и проверять всё равно через RPC, как
раньше. Если поток отвалится, бот продолжит работать по опросу и ничего не
заметит, кроме потерянной скорости.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator

import aiohttp

log = logging.getLogger(__name__)

# Виды L2-сообщений Nitro. Нас интересуют только два: подписанная транзакция и
# пачка из таких же сообщений.
KIND_BATCH = 3
KIND_SIGNED_TX = 4
MAX_BATCH_DEPTH = 4          # вложенность пачек ограничена и самим протоколом

# Где в списке полей транзакции лежит адрес получателя. Для типизированных
# конвертов первый байт — номер типа, дальше обычный RLP-список.
TO_INDEX_LEGACY = 3
TO_INDEX_2930 = 4
TO_INDEX_1559 = 5            # то же место у blob (0x03) и 7702 (0x04)
DATA_AFTER_TO = 2            # между получателем и данными лежит сумма перевода

# Паузы между попытками. Обрыв уже установленной связи — обычное дело, сюда
# можно возвращаться быстро. Отказ в рукопожатии — совсем другое: у публичных
# лент стоит ограничитель частоты, и упорные попытки только продлевают отказ.
# Поэтому после отказа паузы растут до часа, а просьбу сервера подождать
# (Retry-After) мы исполняем буквально.
RECONNECT_STEPS = (1.0, 2.0, 5.0, 15.0, 30.0)
REFUSAL_STEPS = (60.0, 300.0, 900.0, 1800.0, 3600.0)
MAX_WAIT = 3600.0
# Связь, прожившая меньше этого, пользы не принесла: считаем попытку неудачной,
# иначе быстрый разрыв сразу после подключения крутил бы цикл без пауз.
MIN_SESSION_SECONDS = 10.0

# Молчание при живом соединении. Секвенсор говорит без умолку, поэтому полторы
# минуты тишины означают не затишье в сети, а что нас слушают не там.
SILENT_SECONDS = 90.0
# Где у ленты дверь. У одних она в корне адреса, у других — в /feed; угадать
# снаружи нельзя, поэтому пробуем по очереди, пока не пойдут данные.
PATH_VARIANTS = ("", "/feed")
# Кадры с данными. Ленты шлют то текст, то двоичное — с одинаковым JSON внутри,
# и отбрасывать двоичные значило бы молча выкидывать всё содержимое.
DATA_TYPES = (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY)


class FeedSilent(RuntimeError):
    """Соединение есть, данных нет. Обычно это значит: не тот путь."""


def retry_after(error: BaseException) -> float:
    """Сколько секунд сервер просил подождать. 0 — не просил или сказал датой."""
    headers = getattr(error, "headers", None)
    if not headers:
        return 0.0
    try:
        value = headers.get("Retry-After") or headers.get("retry-after") or ""
    except AttributeError:
        return 0.0
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return 0.0
    return min(max(seconds, 0.0), MAX_WAIT)

HANDSHAKE_HEADERS = {"Arbitrum-Feed-Client-Version": "2"}
# Ленты Nitro переходят на обязательное сжатие: клиент, который не предложил
# permessage-deflate, получает отказ ещё на рукопожатии. 15 — размер окна.
COMPRESS_BITS = 15

# Заслоны вроде Cloudflare часто отказывают клиентам, не похожим на браузер:
# aiohttp представляется «Python/aiohttp», и этого достаточно для 403.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"),
    "Origin": "https://robinhood.com",
}

# Чем пробовать, если обычное подключение отказали. Заголовки лежат парами, а не
# словарём, чтобы набор вариантов оставался сравнимым и без повторов.
_NITRO = tuple(HANDSHAKE_HEADERS.items())
_BROWSER = tuple({**HANDSHAKE_HEADERS, **BROWSER_HEADERS}.items())
PROBE_VARIANTS = (
    ("сжатие + заголовки", COMPRESS_BITS, _NITRO, ""),
    ("сжатие, без заголовков", COMPRESS_BITS, (), ""),
    ("без сжатия + заголовки", 0, _NITRO, ""),
    ("как браузер", COMPRESS_BITS, _BROWSER, ""),
    ("как браузер, без сжатия", 0, _BROWSER, ""),
    ("путь /feed", COMPRESS_BITS, _NITRO, "/feed"),
    ("путь /feed как браузер", COMPRESS_BITS, _BROWSER, "/feed"),
)


# Признаки заглушки из инструкции. Только то, чего в настоящем имени сервера не
# бывает: у провайдеров имена собраны из случайных слов, и «example» или «your»
# там встречаются по-настоящему — ложный отказ хуже пропущенной опечатки.
PLACEHOLDER_MARKERS = ("<", ">", "…", "...")


def url_problem(url: str) -> str:
    """Что не так с адресом узла. Пустая строка — адрес выглядит настоящим."""
    value = (url or "").strip()
    if not value:
        return "адрес пустой"
    if not value.startswith(("ws://", "wss://")):
        return ("адрес должен начинаться с wss:// (или ws:// для своего узла), "
                f"а начинается с «{value.split('://')[0][:12]}»")
    host = value.split("://", 1)[1].split("/", 1)[0]
    if not host:
        return "в адресе нет имени сервера"
    if " " in value:
        return "в адресе есть пробел — скорее всего скопировалось лишнее"
    if not host.isascii():
        return "имя сервера написано не латиницей — похоже, это заглушка из примера"
    # Заглушки ищем только в имени сервера: в ключе из пути может случайно
    # оказаться любое сочетание букв, и ложный отказ хуже пропущенной опечатки.
    if any(marker in host for marker in PLACEHOLDER_MARKERS):
        return "это пример из инструкции, а не настоящий адрес — подставьте свой"
    return ""


# ------------------------------------------------------------------ разбор
def parse_frame(payload: str | bytes) -> list[tuple[int, bytes]]:
    """Кадр потока → список (номер блока, тело L2-сообщения)."""
    try:
        frame = json.loads(payload)
    except (TypeError, ValueError):
        return []
    found: list[tuple[int, bytes]] = []
    for item in frame.get("messages") or ():
        if not isinstance(item, dict):
            continue
        inner = (item.get("message") or {}).get("message") or {}
        raw = inner.get("l2Msg")
        if not raw:
            continue
        try:
            body = base64.b64decode(raw)
        except Exception:  # noqa: BLE001 - битый кадр не должен ронять подписку
            continue
        try:
            sequence = int(item.get("sequenceNumber") or 0)
        except (TypeError, ValueError):
            sequence = 0
        found.append((sequence, body))
    return found


def iter_transactions(message: bytes, depth: int = 0) -> Iterator[bytes]:
    """Подписанные транзакции из L2-сообщения, разворачивая вложенные пачки."""
    if not message or depth > MAX_BATCH_DEPTH:
        return
    kind, body = message[0], message[1:]
    if kind == KIND_SIGNED_TX:
        yield body
        return
    if kind != KIND_BATCH:
        return          # служебные сообщения нас не касаются
    position = 0
    while position + 8 <= len(body):
        size = int.from_bytes(body[position:position + 8], "big")
        position += 8
        if size <= 0 or position + size > len(body):
            return      # обрезанная пачка: дальше читать нечего
        yield from iter_transactions(body[position:position + size], depth + 1)
        position += size


def _header(raw: bytes, index: int) -> tuple[int, int, bool]:
    """Границы одного элемента RLP: (начало данных, длина, это список)."""
    prefix = raw[index]
    if prefix < 0x80:
        return index, 1, False                      # число меньше 128 — само себе данные
    if prefix <= 0xB7:
        return index + 1, prefix - 0x80, False
    if prefix <= 0xBF:
        size = prefix - 0xB7
        return index + 1 + size, int.from_bytes(raw[index + 1:index + 1 + size], "big"), False
    if prefix <= 0xF7:
        return index + 1, prefix - 0xC0, True
    size = prefix - 0xF7
    return index + 1 + size, int.from_bytes(raw[index + 1:index + 1 + size], "big"), True


def tx_call(raw: bytes) -> tuple[bytes, bytes] | None:
    """Кому адресована транзакция и что у неё вызывают.

    Возвращает (адрес получателя, первые четыре байта данных) — этого хватает,
    чтобы понять и касается ли транзакция нас, и зачем она. Полностью
    декодировать конверт незачем: остальное при необходимости прочитает RPC.
    None — разбор не удался или это развёртывание контракта.
    """
    if not raw:
        return None
    first = raw[0]
    if first >= 0xC0:
        payload, index = raw, TO_INDEX_LEGACY
    elif first == 0x01:
        payload, index = raw[1:], TO_INDEX_2930
    elif first in {0x02, 0x03, 0x04}:
        payload, index = raw[1:], TO_INDEX_1559
    else:
        return None

    try:
        start, length, is_list = _header(payload, 0)
        if not is_list:
            return None
        position, limit = start, min(len(payload), start + length)
        # Поле данных стоит через одно после получателя (между ними сумма
        # перевода) — и так во всех видах конвертов, поэтому смещение общее.
        for wanted in range(index + DATA_AFTER_TO + 1):
            item_start, item_length, _kind = _header(payload, position)
            if wanted == index:
                if item_length != 20:
                    return None      # пустое поле — это развёртывание контракта
                address = payload[item_start:item_start + item_length]
            if wanted == index + DATA_AFTER_TO:
                return address, bytes(payload[item_start:item_start + min(4, item_length)])
            position = item_start + item_length
            if position > limit:
                return None
    except (IndexError, UnboundLocalError):
        return None
    return None


def tx_target(raw: bytes) -> bytes | None:
    """Только адрес получателя."""
    call = tx_call(raw)
    return call[0] if call else None


def targets(message: bytes) -> set[bytes]:
    """Все адреса получателей в одном L2-сообщении."""
    return {address for address, _selector in calls(message)}


def calls(message: bytes) -> set[tuple[bytes, bytes]]:
    """Все пары «получатель, вызываемый метод» в одном L2-сообщении."""
    found = set()
    for raw in iter_transactions(message):
        call = tx_call(raw)
        if call is not None:
            found.add(call)
    return found


# ------------------------------------------------------------------ подписка
async def probe(url: str, *, timeout: float = 10.0) -> list[tuple[str, str]]:
    """Перебирает способы подключения и говорит, какой сработал.

    Ленты разных сетей требуют разного: где-то обязательно сжатие, где-то свой
    путь. Гадать об этом по одному «403» бессмысленно, поэтому проверка
    перебирает варианты сама и показывает ответ сервера по каждому.
    """
    results: list[tuple[str, str]] = []
    base = url.rstrip("/")
    async with aiohttp.ClientSession() as session:
        # Сначала обычный HTTPS: так видно, кто вообще отвечает — лента или
        # чужой заслон вроде Cloudflare.
        page = base.replace("wss://", "https://").replace("ws://", "http://")
        try:
            async with session.get(page, headers=BROWSER_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as answer:
                server = answer.headers.get("Server", "")
                results.append(("обычный HTTPS-запрос",
                                f"{answer.status}" + (f" · сервер {server}" if server else "")))
        except Exception as exc:  # noqa: BLE001
            results.append(("обычный HTTPS-запрос", f"не ответил: {str(exc)[:80]}"))

        for title, compress, headers, path in PROBE_VARIANTS:
            target = base + path
            try:
                async with session.ws_connect(
                    target,
                    headers=dict(headers),
                    compress=compress,
                    timeout=aiohttp.ClientWSTimeout(ws_close=timeout),
                    max_msg_size=32 * 1024 * 1024,
                ) as socket:
                    # Рукопожатие прошло — это уже ответ на главный вопрос. Данные
                    # лента шлёт сама, а вот RPC молчит, пока его не попросят:
                    # молчание здесь не отказ, и путать одно с другим нельзя.
                    try:
                        frame = await asyncio.wait_for(socket.receive(), timeout=min(timeout, 5.0))
                        got = (len(parse_frame(frame.data))
                               if frame.type is aiohttp.WSMsgType.TEXT else 0)
                        note = f"сообщений в первом кадре: {got}"
                    except TimeoutError:
                        note = "данных без подписки не шлёт — для RPC это норма"
                    results.append((title, f"✅ подключился, {note}"))
                    return results       # рабочий способ найден, дальше не нужно
            except Exception as exc:  # noqa: BLE001 - перебор, отказ это ожидаемый исход
                results.append((title, f"⛔️ {str(exc)[:90]}"))
    return results


class SequencerFeed:
    """Держит соединение с потоком секвенсора и будит бота при попадании."""

    def __init__(self, url: str, watched: set[str],
                 on_hit: Callable[[int], Awaitable[None] | None], *, name: str = "",
                 compress: int = COMPRESS_BITS, headers: dict[str, str] | None = None,
                 selectors: frozenset[bytes] | set[bytes] | None = None) -> None:
        self.url = url
        self.compress = compress
        self.headers = HANDSHAKE_HEADERS if headers is None else headers
        self.name = name or url
        self.on_hit = on_hit
        # Адреса храним байтами: сравнивать их придётся на каждую транзакцию.
        self.watched = {bytes.fromhex(address.lower().removeprefix("0x"))
                        for address in watched if address}
        # Какие вызовы считать своими. Пусто — любые: так поток годится и для
        # адреса, у которого интересна вся переписка.
        self.selectors = frozenset(selectors) if selectors else frozenset()
        self._running = False
        self.connected = False
        self.messages = 0        # сколько кадров разобрали
        self.hits = 0            # сколько раз поток нас разбудил
        self.last_sequence = 0
        self.last_error = ""
        self.refusals = 0        # подряд отказов в рукопожатии
        self.waiting = 0.0       # сколько ждём до следующей попытки
        self.frames = 0          # сколько кадров пришло — отдельно от разобранных
        self._variant = 0        # какой путь пробуем сейчас

    async def run(self) -> None:
        self._running = True
        if not self.watched:
            log.info("Поток %s: следить не за чем — подписка не нужна", self.name)
            return
        log.info("Поток секвенсора %s: подключаюсь (адресов под наблюдением %s)",
                 self.name, len(self.watched))
        failures = 0
        async with aiohttp.ClientSession() as session:
            while self._running:
                started = time.monotonic()
                try:
                    await self._listen(session)
                    # Долгая связь — признак, что с нами всё в порядке, счётчики
                    # можно обнулить. Мгновенный разрыв пользы не принёс.
                    if time.monotonic() - started >= MIN_SESSION_SECONDS:
                        failures = self.refusals = 0
                    else:
                        failures += 1
                    wait = RECONNECT_STEPS[min(failures, len(RECONNECT_STEPS) - 1)]
                except asyncio.CancelledError:
                    raise
                except FeedSilent as quiet:
                    # Пустили, но молчат. Отказом это не считается — ограничитель
                    # частоты здесь ни при чём, — поэтому возвращаемся быстро,
                    # но уже в другую дверь.
                    self.connected = False
                    self._variant += 1
                    self.last_error = "пустили, но данных нет — пробую другой путь"
                    failures += 1
                    wait = RECONNECT_STEPS[min(failures, len(RECONNECT_STEPS) - 1)]
                    log.warning("Поток %s: %s молчит, перехожу на %s",
                                self.name, quiet, self.target())
                except aiohttp.WSServerHandshakeError as exc:
                    # Нас не приняли. Ломиться дальше в прежнем темпе — верный
                    # способ остаться в отказе навсегда: счётчик ограничителя
                    # обнуляется на каждой попытке.
                    self.connected = False
                    self.last_error = f"отказ, код {exc.status}"
                    asked = retry_after(exc)
                    wait = max(asked, REFUSAL_STEPS[min(self.refusals,
                                                        len(REFUSAL_STEPS) - 1)])
                    self.refusals += 1
                    log.warning("Поток %s: %s, следующая попытка через %.0f мин",
                                self.name, self.last_error, wait / 60)
                except Exception as exc:  # noqa: BLE001 - обрыв связи это норма
                    self.connected = False
                    self.last_error = str(exc)[:200]
                    failures += 1
                    wait = RECONNECT_STEPS[min(failures, len(RECONNECT_STEPS) - 1)]
                    log.warning("Поток %s: обрыв (%s)", self.name, self.last_error)
                if not self._running:
                    break
                self.waiting = wait
                await asyncio.sleep(wait)

    def stop(self) -> None:
        self._running = False

    def target(self) -> str:
        """Адрес текущей попытки: корень или /feed."""
        return self.url.rstrip("/") + PATH_VARIANTS[self._variant % len(PATH_VARIANTS)]

    async def _listen(self, session: aiohttp.ClientSession) -> None:
        target = self.target()
        async with session.ws_connect(target, headers=self.headers, compress=self.compress,
                                      heartbeat=30, max_msg_size=32 * 1024 * 1024) as socket:
            self.connected = True
            self.last_error = ""
            log.info("Поток %s: подключён (%s)", self.name, target)
            # Время последних данных, а не последнего кадра вообще. Раз в
            # полминуты мы сами шлём служебный пинг, и ответ на него приходит
            # кадром: считать его признаком жизни ленты значит никогда не
            # заметить, что данных нет.
            fed = time.monotonic()
            while self._running:
                left = SILENT_SECONDS - (time.monotonic() - fed)
                if left <= 0:
                    raise FeedSilent(target)
                try:
                    frame = await asyncio.wait_for(socket.receive(), timeout=left)
                except TimeoutError:
                    raise FeedSilent(target) from None
                if frame.type not in DATA_TYPES:
                    if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                      aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                fed = time.monotonic()
                self.frames += 1
                if self.frames == 1:
                    # Первый кадр стоит показать в журнале целиком по размеру и
                    # виду: если разбор не пойдёт, именно это скажет почему.
                    log.info("Поток %s: первый кадр — %s, %s байт",
                             self.name, frame.type.name, len(frame.data))
                await self.handle(frame.data)
        self.connected = False

    async def handle(self, payload: str | bytes) -> bool:
        """Разбирает кадр и будит бота, если тронули наши адреса."""
        hit = False
        sequence = 0
        for number, message in parse_frame(payload):
            self.messages += 1
            sequence = max(sequence, number)
            if self._touches_us(message):
                hit = True
        if sequence:
            self.last_sequence = max(self.last_sequence, sequence)
        if not hit:
            return False
        self.hits += 1
        try:
            result = self.on_hit(sequence)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 - подписка важнее одного пробуждения
            log.debug("Поток %s: обработчик не сработал (%s)", self.name, exc)
        return True

    def _touches_us(self, message: bytes) -> bool:
        """Есть ли в сообщении интересная нам транзакция."""
        for address, selector in calls(message):
            if address not in self.watched:
                continue
            if not self.selectors or selector in self.selectors:
                return True
        return False

    def status(self) -> str:
        """Строка для /health."""
        if not self.watched:
            return "выключен"
        if self.connected:
            state = "на связи"
        else:
            state = f"нет связи ({self.last_error or 'подключаюсь'})"
            if self.waiting >= 60:
                state += f", следующая попытка через {self.waiting / 60:.0f} мин"
        return (f"{state} · кадров {self.frames} · блок {self.last_sequence}"
                f" · попаданий {self.hits}")
