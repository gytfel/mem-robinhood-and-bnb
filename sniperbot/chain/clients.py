"""Асинхронные RPC-клиенты сетей с автоматическим переключением эндпоинтов."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import aiohttp
from web3 import AsyncHTTPProvider, AsyncWeb3
from web3.contract import AsyncContract
from web3.exceptions import ContractLogicError

from sniperbot.chain.abi import ERC20_ABI, FACTORY_ABI, PAIR_ABI, ROUTER_ABI
from sniperbot.config import ChainConfig
from sniperbot.utils.evm import to_checksum
from sniperbot.utils.fmt import plural

try:  # web3 >= 7
    from web3.middleware import ExtraDataToPOAMiddleware as _POA_MIDDLEWARE
except ImportError:  # pragma: no cover - web3 6.x
    from web3.middleware import geth_poa_middleware as _POA_MIDDLEWARE  # type: ignore[attr-defined]

log = logging.getLogger(__name__)

T = TypeVar("T")

RPC_TIMEOUT = 12

# Ошибки, при которых имеет смысл сходить в другой RPC.
TRANSPORT_ERRORS = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError, aiohttp.ClientError)

# Причины отказа. Их стоит различать: «нода просит сбавить темп» лечится
# паузой, «доступ закрыт» — ключом в .env, а «слишком широкий запрос логов» —
# вообще не поломка ноды, и перебирать из-за него остальные бессмысленно.
RATE_LIMIT = "rate_limit"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"
FORBIDDEN = "forbidden"
SERVER = "server"
LOG_RANGE = "log_range"
OTHER = "other"

REASON_TITLES = {
    RATE_LIMIT: "лимит запросов",
    TIMEOUT: "не дождались ответа",
    UNREACHABLE: "нет связи",
    FORBIDDEN: "доступ закрыт (ключ или тариф)",
    SERVER: "ошибка на стороне ноды",
    LOG_RANGE: "слишком широкий запрос логов",
    OTHER: "прочее",
}

REASON_HINTS = {
    RATE_LIMIT: "узел режет частоту. Бот сам сбавляет темп, но надёжнее добавить "
                "второй эндпоинт в RPC_URLS или поднять тариф",
    FORBIDDEN: "узел не принимает запросы: проверьте ключ в ссылке и тариф",
    UNREACHABLE: "до узла нет связи — уберите его из RPC_URLS, если это надолго",
    TIMEOUT: "узел отвечает дольше 12 секунд — для снайпа он бесполезен",
    SERVER: "у провайдера неполадки; если это постоянно — смените узел",
    LOG_RANGE: "узел не отдаёт логи таким куском; бот уменьшает шаг сам",
}

# Коды состояния ищем как отдельное число: иначе «500» находится внутри
# «15000000» из сообщения про газ, и обычная ошибка контракта выглядит как
# падение ноды.
_STATUS_RE = re.compile(r"\b([45]\d\d)\b")

# Специфичные формулировки — до общих: «limit exceeded» встречается и у лимита
# частоты, и у слишком широкого eth_getLogs.
_LOG_RANGE_MARKERS = (
    "more than", "block range", "range is too", "range too", "log response size",
    "query returned more than", "too many blocks", "exceed maximum block",
)
_RATE_MARKERS = (
    "too many requests", "rate limit", "rate-limit", "request limit", "limit exceeded",
    "quota", "capacity", "credits", "throttl",
)
_FORBIDDEN_MARKERS = ("forbidden", "unauthorized", "invalid api key", "access denied")
_SERVER_MARKERS = ("service unavailable", "temporarily unavailable", "bad gateway",
                   "internal error", "internal server error")


def _http_status(exc: BaseException) -> int:
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else 0


def classify(exc: BaseException) -> str:
    """Почему узел не ответил. Пустая строка — это не он, а сам запрос.

    Ошибку контракта повторять на других нодах бессмысленно: они ответят так
    же, а счётчик сбоев наберёт чужих отказов и перестанет что-либо значить.
    """
    if isinstance(exc, ContractLogicError):
        return ""

    text = str(exc).lower()
    status = _http_status(exc)

    if any(marker in text for marker in _LOG_RANGE_MARKERS):
        return LOG_RANGE
    if status == 429 or any(marker in text for marker in _RATE_MARKERS):
        return RATE_LIMIT
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in text or "timed out" in text:
        return TIMEOUT
    if status in {401, 403} or any(marker in text for marker in _FORBIDDEN_MARKERS):
        return FORBIDDEN
    if 500 <= status <= 599 or any(marker in text for marker in _SERVER_MARKERS):
        return SERVER
    if status == 404 or status == 408:
        return SERVER
    if isinstance(exc, TRANSPORT_ERRORS) or "connection" in text or "unreachable" in text:
        return UNREACHABLE
    return ""


def is_transport_error(exc: BaseException) -> bool:
    """Отличает «нода недоступна» от «контракт вернул ошибку»."""
    return bool(classify(exc))


def _block_span(params: dict) -> tuple[int | None, int | None]:
    """Границы диапазона из параметров eth_getLogs — числом, а не «latest»."""

    def number(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.startswith("0x"):
            try:
                return int(value, 16)
            except ValueError:
                return None
        return None

    return number(params.get("fromBlock")), number(params.get("toBlock"))


class ChainUnavailable(RuntimeError):
    """Ни один RPC сети не отвечает."""


# Сколько не трогать узел, который только что отказал: чем дольше он молчит,
# тем реже его стоит спрашивать. Мёртвый эндпоинт в списке не должен отнимать
# время у каждого запроса.
COOLDOWN_STEPS = (5.0, 15.0, 45.0, 120.0)
RATE_LIMIT_PAUSE = 0.5      # лимит частоты снимается мгновенно — есть смысл подождать
MIN_PACE, MAX_PACE = 0.05, 2.0
HEAD_CACHE_SECONDS = 0.4    # столько номер последнего блока считается свежим
PACE_RELEASE_AFTER = 40     # успехов подряд, после которых темп снова ускоряется


@dataclass
class Endpoint:
    """Состояние одного RPC-узла: что с ним было и когда его спрашивать.

    Узел, который режет частоту, не сломан — ему просто нельзя задавать
    вопросы так часто. Поэтому здесь два разных механизма: `pace` растягивает
    запросы во времени, а `resume_at` временно выводит узел из строя.
    """

    url: str
    requests: int = 0
    failures: int = 0
    reasons: Counter = field(default_factory=Counter)
    last_error: str = ""
    streak: int = 0            # отказов подряд
    wins: int = 0              # успехов подряд
    resume_at: float = 0.0     # до этого момента узел не трогаем
    pace: float = 0.0          # минимальный интервал между запросами, сек
    free_at: float = 0.0       # когда можно отправить следующий запрос

    def ready(self, now: float) -> bool:
        return now >= self.resume_at

    def note_success(self) -> None:
        self.requests += 1
        self.streak = 0
        self.resume_at = 0.0
        self.wins += 1
        if self.pace and self.wins >= PACE_RELEASE_AFTER:
            # Узел давно не жаловался — возвращаем скорость: снайпу каждая
            # десятая доля секунды дороже, чем нам спокойствие.
            self.pace = 0.0 if self.pace <= MIN_PACE else max(MIN_PACE, self.pace * 0.7)
            self.wins = 0

    def note_failure(self, reason: str, error: BaseException, now: float) -> None:
        self.requests += 1
        self.failures += 1
        self.wins = 0
        self.reasons[reason] += 1
        self.last_error = str(error)[:200]
        self.streak += 1
        if reason == RATE_LIMIT:
            # Частоту режут — значит спрашиваем чаще, чем позволено. Выводить
            # такой узел из строя нельзя (другого может не быть), его надо
            # просто спрашивать реже.
            self.pace = min(MAX_PACE, max(MIN_PACE, self.pace * 2 or MIN_PACE))
            self.resume_at = now + min(RATE_LIMIT_PAUSE, self.pace)
            return
        if reason == LOG_RANGE:
            return          # не вина узла: запрос переспросят меньшим куском
        self.resume_at = now + COOLDOWN_STEPS[min(self.streak, len(COOLDOWN_STEPS)) - 1]

    def health(self) -> str:
        """Строка для /usage: сколько отказов и по каким причинам."""
        asked = f"{self.requests} {plural(self.requests, 'запрос', 'запроса', 'запросов')}"
        if not self.failures:
            return f"{asked}, сбоев нет"
        top = ", ".join(f"{REASON_TITLES.get(reason, reason)} {count}"
                        for reason, count in self.reasons.most_common(3))
        return f"{asked}, сбоев {self.failures} ({top})"


class ChainClient:
    """Обёртка над несколькими RPC одной сети.

    Любой запрос выполняется через :meth:`run`: при сетевой ошибке клиент
    переключается на следующий эндпоинт, а ошибки контракта (revert)
    пробрасываются наверх без повторов.
    """

    def __init__(self, config: ChainConfig) -> None:
        if not config.rpc_urls:
            raise ChainUnavailable(f"Для сети {config.key} не задан ни один RPC URL")
        self.config = config
        self._providers: list[AsyncWeb3] = [self._make_w3(url) for url in config.rpc_urls]
        self._index = 0
        self._lock = asyncio.Lock()
        self.endpoints = [Endpoint(url) for url in config.rpc_urls]
        self.requests = 0        # счётчик запросов — для /usage
        self.failures = 0        # отказов узлов; почти все переживает переключение
        self.dropped = 0         # запросов, которые так и не удалось выполнить
        self.log_span_limit = 0  # посильный узлу кусок блоков для eth_getLogs (0 — без ограничения)
        self._head: tuple[float, int] = (0.0, 0)

    # ------------------------------------------------------------------ setup
    def _make_w3(self, url: str) -> AsyncWeb3:
        w3 = AsyncWeb3(AsyncHTTPProvider(url, request_kwargs={"timeout": RPC_TIMEOUT}))
        if self.config.poa:
            w3.middleware_onion.inject(_POA_MIDDLEWARE, layer=0)
        return w3

    @property
    def w3(self) -> AsyncWeb3:
        return self._providers[self._index]

    @property
    def rpc_url(self) -> str:
        return self.config.rpc_urls[self._index]

    def _now(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:          # вне цикла событий — в тестах и утилитах
            return 0.0

    def _order(self, now: float) -> list[int]:
        """Кого спрашивать и в каком порядке: рабочий узел первым.

        Узел на «отдыхе» пропускается, но если отдыхают все, берём того, чей
        отдых кончится раньше: остаться совсем без сети хуже, чем потревожить
        уставшую ноду.
        """
        order = [(self._index + offset) % len(self._providers)
                 for offset in range(len(self._providers))]
        ready = [index for index in order if self.endpoints[index].ready(now)]
        if ready:
            return ready
        return sorted(order, key=lambda index: self.endpoints[index].resume_at)

    async def _pace(self, endpoint: Endpoint) -> None:
        """Растягивает запросы к узлу, который жаловался на частоту."""
        if endpoint.pace <= 0:
            return
        now = self._now()
        wait = endpoint.free_at - now
        endpoint.free_at = max(now, endpoint.free_at) + endpoint.pace
        if wait > 0:
            await asyncio.sleep(min(wait, MAX_PACE))

    async def run(self, fn: Callable[[AsyncWeb3], Awaitable[T]], *, _retry: bool = False) -> T:
        """Выполняет запрос, перебирая RPC при транспортных ошибках."""
        last_error: Exception | None = None
        if not _retry:
            self.requests += 1
        for index in self._order(self._now()):
            endpoint = self.endpoints[index]
            await self._pace(endpoint)
            try:
                result = await fn(self._providers[index])
            except ContractLogicError:
                raise
            except Exception as exc:  # noqa: BLE001 - провайдеры кидают свои типы ошибок
                reason = classify(exc)
                if not reason:
                    raise
                if reason == LOG_RANGE:
                    # Остальные узлы ответят так же: это ограничение на размер
                    # ответа, а не поломка. Запрос переспросят меньшим куском.
                    endpoint.note_failure(reason, exc, self._now())
                    raise
                last_error = exc
                self.failures += 1
                endpoint.note_failure(reason, exc, self._now())
                log.debug("RPC %s: %s (%s)", endpoint.url, REASON_TITLES.get(reason, reason), exc)
                continue
            else:
                endpoint.note_success()
                if index != self._index:
                    log.info("Сеть %s: переключился на RPC %s", self.config.key, endpoint.url)
                    self._index = index
                return result

        # Отказали все. Если дело в частоте, ждать осмысленно: лимит снимается
        # через мгновение, а запасного узла может не быть вовсе.
        if not _retry and last_error is not None and classify(last_error) == RATE_LIMIT:
            await asyncio.sleep(RATE_LIMIT_PAUSE)
            return await self.run(fn, _retry=True)

        self.dropped += 1
        raise ChainUnavailable(
            f"Все RPC сети {self.config.name} недоступны: {last_error}"
        ) from last_error

    async def close(self) -> None:
        """Закрывает HTTP-сессии провайдеров (иначе aiohttp ругается на выходе)."""
        for provider in self._providers:
            disconnect = getattr(provider.provider, "disconnect", None)
            if disconnect is None:
                continue
            try:
                await disconnect()
            except Exception as exc:  # noqa: BLE001 - на выходе это не важно
                log.debug("Не смог закрыть провайдера: %s", exc)

    async def healthcheck(self) -> bool:
        try:
            block = await self.run(lambda w3: w3.eth.get_block_number())
        except Exception as exc:  # noqa: BLE001
            log.warning("Сеть %s не отвечает: %s", self.config.key, exc)
            return False
        log.info("Сеть %s онлайн, блок %s (%s)", self.config.name, block, self.rpc_url)
        return True

    # -------------------------------------------------------------- contracts
    def erc20(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=ERC20_ABI)

    def router(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=ROUTER_ABI)

    def factory(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=FACTORY_ABI)

    def pair(self, address: str, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=PAIR_ABI)

    def contract(self, address: str, abi: list, w3: AsyncWeb3 | None = None) -> AsyncContract:
        return (w3 or self.w3).eth.contract(address=to_checksum(address), abi=abi)

    async def call(self, address: str, abi: list, fn_name: str, *args, **call_kwargs) -> Any:
        """Вызов view-функции произвольного контракта с failover."""

        async def _do(w3: AsyncWeb3) -> Any:
            contract = self.contract(address, abi, w3)
            return await contract.functions[fn_name](*args).call(**call_kwargs)

        return await self.run(_do)

    async def call_all(self, address: str, abi: list, fn_name: str, *args) -> list:
        """Тот же вызов на всех RPC сразу — возвращает удачные ответы.

        Нужен там, где ошибочный ответ дороже лишнего запроса. Отставшая или
        подрезанная нода отвечает нулём без всякой ошибки, и по одному такому
        ответу нельзя решать судьбу позиции.
        """
        results = []
        for index, provider in enumerate(self._providers):
            try:
                contract = self.contract(address, abi, provider)
                results.append(await contract.functions[fn_name](*args).call())
            except ContractLogicError:
                raise
            except Exception as exc:  # noqa: BLE001 - опрашиваем остальные
                log.debug("RPC %s не ответил на %s: %s", self.config.rpc_urls[index], fn_name, exc)
        return results

    # ------------------------------------------------------------------ chain
    async def block_number(self) -> int:
        """Номер последнего блока, с кешем на долю секунды.

        Сканеров у сети столько, сколько площадок, и каждый спрашивает голову
        сам. Ответ устаревает медленнее, чем они успевают спросить: блок за
        это время не меняется, а запросов к ноде втрое меньше.
        """
        now = self._now()
        stamp, cached = self._head
        if cached and now - stamp < HEAD_CACHE_SECONDS:
            return cached
        value = await self.run(lambda w3: w3.eth.get_block_number())
        self._head = (now, value)
        return value

    async def native_balance(self, address: str) -> int:
        checksum = to_checksum(address)
        return await self.run(lambda w3: w3.eth.get_balance(checksum))

    async def get_logs(self, params: dict) -> list:
        """Логи за диапазон блоков, с подбором посильного шага.

        Узлы ограничивают размер ответа по-разному, и «слишком широкий запрос»
        лечится не переключением ноды, а половинным куском. Рабочий шаг
        запоминается, чтобы не натыкаться на ту же стену каждый опрос.
        """
        try:
            return await self.run(lambda w3: w3.eth.get_logs(params))
        except Exception as exc:  # noqa: BLE001 - дробим только отказ по размеру
            if classify(exc) != LOG_RANGE:
                raise
            start, stop = _block_span(params)
            if start is None or stop is None or stop <= start:
                raise
            middle = (start + stop) // 2
            # Запоминаем посильный кусок, чтобы в следующий опрос не биться
            # в ту же стену: сканер спросит сразу столько, сколько влезает.
            self.log_span_limit = max(1, middle - start + 1)
            log.info("Сеть %s: узел не отдаёт логи за %s блоков — беру по %s",
                     self.config.key, stop - start + 1, self.log_span_limit)
            first = await self.get_logs({**params, "fromBlock": start, "toBlock": middle})
            second = await self.get_logs({**params, "fromBlock": middle + 1, "toBlock": stop})
            return first + second

    async def transaction_count(self, address: str, block: str = "pending") -> int:
        checksum = to_checksum(address)
        return await self.run(lambda w3: w3.eth.get_transaction_count(checksum, block))

    async def send_raw(self, raw_tx: bytes) -> str:
        tx_hash = await self.run(lambda w3: w3.eth.send_raw_transaction(raw_tx))
        return tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)

    async def wait_receipt(self, tx_hash: str, timeout: float = 120.0, poll: float = 1.0) -> dict:
        """Ожидание квитанции без привязки к конкретному провайдеру."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                receipt = await self.run(lambda w3: w3.eth.get_transaction_receipt(tx_hash))
                if receipt is not None:
                    return dict(receipt)
            except Exception:  # noqa: BLE001 - "not found" до попадания в блок
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Транзакция {tx_hash} не подтвердилась за {timeout:.0f} c")
            await asyncio.sleep(poll)

    async def gas_fees(self, multiplier: float = 1.0, priority_gwei: float = 1.0) -> dict:
        """Параметры газа для транзакции с учётом типа сети."""
        if self.config.eip1559:
            block = await self.run(lambda w3: w3.eth.get_block("latest"))
            base_fee = int(block.get("baseFeePerGas") or 0)
            if base_fee:
                priority = int(priority_gwei * 1e9)
                max_fee = int((base_fee * 2 + priority) * multiplier)
                return {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": priority}
        gas_price = await self.run(lambda w3: w3.eth.gas_price)
        return {"gasPrice": int(gas_price * multiplier)}

    async def estimate_gas(self, tx: dict) -> int:
        return await self.run(lambda w3: w3.eth.estimate_gas(tx))

    async def raw_call(self, tx: dict, state_override: dict | None = None, block: str = "latest") -> bytes:
        """eth_call с необязательным state override (нужен симулятору)."""

        async def _do(w3: AsyncWeb3) -> bytes:
            if state_override:
                return await w3.eth.call(tx, block, state_override)
            return await w3.eth.call(tx, block)

        return await self.run(_do)


class ChainRegistry:
    """Пул клиентов по всем активным сетям."""

    def __init__(self, chains: dict[str, ChainConfig]) -> None:
        self._configs = chains
        self._clients: dict[str, ChainClient] = {}

    def get(self, key: str) -> ChainClient:
        if key not in self._clients:
            config = self._configs.get(key)
            if config is None:
                raise KeyError(f"Сеть {key} не сконфигурирована")
            self._clients[key] = ChainClient(config)
        return self._clients[key]

    def config(self, key: str) -> ChainConfig:
        return self._configs[key]

    @property
    def keys(self) -> list[str]:
        return list(self._configs)

    @property
    def configs(self) -> dict[str, ChainConfig]:
        return dict(self._configs)

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()

    async def healthcheck_all(self) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for key in self._configs:
            try:
                result[key] = await self.get(key).healthcheck()
            except Exception as exc:  # noqa: BLE001
                log.warning("Сеть %s не инициализирована: %s", key, exc)
                result[key] = False
        return result
