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

RECONNECT_STEPS = (1.0, 2.0, 5.0, 15.0, 30.0)
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


def tx_target(raw: bytes) -> bytes | None:
    """Кому адресована транзакция. None — разбор не удался или создание контракта.

    Полностью декодировать транзакцию незачем: из всего конверта нужен один
    адрес, чтобы понять, касается ли она нас. Остальное прочитает RPC.
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
        for _ in range(index):
            item_start, item_length, _kind = _header(payload, position)
            position = item_start + item_length
            if position > limit:
                return None
        item_start, item_length, _kind = _header(payload, position)
        if item_length != 20:
            return None      # пустое поле — это развёртывание контракта
        return payload[item_start:item_start + item_length]
    except IndexError:
        return None


def targets(message: bytes) -> set[bytes]:
    """Все адреса получателей в одном L2-сообщении."""
    found = set()
    for raw in iter_transactions(message):
        address = tx_target(raw)
        if address is not None:
            found.add(address)
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
                 compress: int = COMPRESS_BITS, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.compress = compress
        self.headers = HANDSHAKE_HEADERS if headers is None else headers
        self.name = name or url
        self.on_hit = on_hit
        # Адреса храним байтами: сравнивать их придётся на каждую транзакцию.
        self.watched = {bytes.fromhex(address.lower().removeprefix("0x"))
                        for address in watched if address}
        self._running = False
        self.connected = False
        self.messages = 0        # сколько кадров разобрали
        self.hits = 0            # сколько раз поток нас разбудил
        self.last_sequence = 0
        self.last_error = ""

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
                try:
                    await self._listen(session)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - обрыв связи это норма
                    self.connected = False
                    self.last_error = str(exc)[:200]
                    log.warning("Поток %s: обрыв (%s)", self.name, self.last_error)
                    failures += 1
                if not self._running:
                    break
                await asyncio.sleep(RECONNECT_STEPS[min(failures, len(RECONNECT_STEPS) - 1)])

    def stop(self) -> None:
        self._running = False

    async def _listen(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(self.url, headers=self.headers, compress=self.compress,
                                      heartbeat=30, max_msg_size=32 * 1024 * 1024) as socket:
            self.connected = True
            self.last_error = ""
            log.info("Поток %s: подключён", self.name)
            async for frame in socket:
                if frame.type is not aiohttp.WSMsgType.TEXT:
                    continue
                await self.handle(frame.data)
                if not self._running:
                    break
        self.connected = False

    async def handle(self, payload: str | bytes) -> bool:
        """Разбирает кадр и будит бота, если тронули наши адреса."""
        hit = False
        sequence = 0
        for number, message in parse_frame(payload):
            self.messages += 1
            sequence = max(sequence, number)
            if targets(message) & self.watched:
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

    def status(self) -> str:
        """Строка для /health."""
        if not self.watched:
            return "выключен"
        state = "на связи" if self.connected else f"нет связи ({self.last_error or 'подключаюсь'})"
        return f"{state} · блок {self.last_sequence} · пробуждений {self.hits}"
