"""Статический анализ токена: что скрыто в байт-коде и как распределено предложение.

Эти проверки не требуют симуляции и стоят два-три RPC-вызова, зато отсекают
самые частые способы потерять деньги на новом токене:

* **функция чеканки** — владелец допечатает себе токенов и продаст в пул;
* **чёрный список** — вам запретят продавать уже после покупки;
* **пауза торгов** — то же самое, только через `pause()`;
* **прокси** — контракт можно заменить на honeypot после вашей покупки;
* **концентрация предложения** — если у владельца половина всех токенов,
  цена держится ровно до момента, когда он решит выйти.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from eth_utils import keccak

from sniperbot.chain.abi import ERC20_ABI
from sniperbot.chain.clients import ChainClient
from sniperbot.chain.erc20 import TokenInfo
from sniperbot.utils.evm import ZERO_ADDRESS, to_checksum

log = logging.getLogger(__name__)

# Слоты, куда прокси-контракты кладут адрес реализации.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1822_IMPLEMENTATION_SLOT = "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7"

# Сигнатуры, наличие которых в байт-коде говорит о возможностях владельца.
# Ключ — что это значит для нас, значение — список сигнатур.
RISKY_SIGNATURES: dict[str, tuple[str, ...]] = {
    "mint": (
        "mint(address,uint256)",
        "mint(uint256)",
        "_mint(address,uint256)",
        "mintTo(address,uint256)",
    ),
    "blacklist": (
        "blacklist(address,bool)",
        "setBlacklist(address,bool)",
        "addBotToBlackList(address)",
        "setBots(address[],bool)",
        "setBlackList(address,bool)",
        "blackList(address,bool)",
        "setExcludedFromTrading(address,bool)",
    ),
    "pause": (
        "pause()",
        "setTradingEnabled(bool)",
        "enableTrading(bool)",
        "setTradingOpen(bool)",
    ),
    "fees": (
        "setFees(uint256,uint256)",
        "setTaxes(uint256,uint256)",
        "setTaxFeePercent(uint256)",
        "setBuyTax(uint256)",
        "setSellTax(uint256)",
        "updateFees(uint256,uint256)",
    ),
    "limits": (
        "setMaxTxAmount(uint256)",
        "setMaxWalletAmount(uint256)",
        "setMaxWalletSize(uint256)",
    ),
}

RISK_TITLES = {
    "mint": "чеканка новых токенов",
    "blacklist": "чёрный список кошельков",
    "pause": "остановка торгов",
    "fees": "изменение налогов",
    "limits": "изменение лимитов",
}


def selector(signature: str) -> str:
    """Четырёхбайтовый селектор функции."""
    return keccak(text=signature)[:4].hex()


SELECTORS: dict[str, tuple[str, ...]] = {
    key: tuple(selector(sig) for sig in signatures) for key, signatures in RISKY_SIGNATURES.items()
}


@dataclass(slots=True)
class ContractProfile:
    """Что удалось узнать о токене без симуляции."""

    powers: set[str] = field(default_factory=set)      # ключи из RISKY_SIGNATURES
    is_proxy: bool = False
    owner_share: Decimal | None = None                 # % предложения у владельца
    pool_share: Decimal | None = None                  # % предложения в пуле
    code_size: int = 0

    @property
    def owner_can_hurt(self) -> bool:
        """Есть ли у владельца рычаг, которым он может забрать ваши деньги."""
        return bool(self.powers & {"mint", "blacklist", "pause"}) or self.is_proxy

    def describe(self) -> str:
        parts = [RISK_TITLES[key] for key in sorted(self.powers)]
        if self.is_proxy:
            parts.insert(0, "обновляемый прокси")
        return ", ".join(parts) if parts else "особых прав не нашёл"


def scan_bytecode(code: bytes | str) -> set[str]:
    """Ищет в байт-коде селекторы «опасных» функций.

    Селекторы лежат в коде открытым текстом (диспетчер сравнивает их с calldata),
    поэтому обычного поиска подстроки достаточно — компилировать ничего не нужно.
    """
    if isinstance(code, str):
        code = bytes.fromhex(code.removeprefix("0x"))
    if not code:
        return set()
    hex_code = code.hex()
    found = set()
    for key, selectors in SELECTORS.items():
        if any(item in hex_code for item in selectors):
            found.add(key)
    return found


async def is_proxy(client: ChainClient, address: str) -> bool:
    """Проверяет слоты EIP-1967/EIP-1822: там лежит адрес реализации прокси."""
    checksum = to_checksum(address)
    for slot in (EIP1967_IMPLEMENTATION_SLOT, EIP1822_IMPLEMENTATION_SLOT):
        try:
            raw = await client.run(lambda w3, s=slot: w3.eth.get_storage_at(checksum, s))
        except Exception as exc:  # noqa: BLE001 - нода может не отдавать storage
            log.debug("get_storage_at(%s): %s", address, exc)
            return False
        value = int.from_bytes(bytes(raw), "big") if raw else 0
        if value != 0:
            return True
    return False


async def profile_token(
    client: ChainClient,
    token: TokenInfo,
    *,
    code: bytes | None = None,
    pool_address: str | None = None,
) -> ContractProfile:
    """Собирает статический профиль токена."""
    if code is None:
        code = await client.run(lambda w3: w3.eth.get_code(to_checksum(token.address)))

    profile = ContractProfile(powers=scan_bytecode(code), code_size=len(code or b""))
    profile.is_proxy = await is_proxy(client, token.address)

    supply = Decimal(token.total_supply or 0)
    if supply > 0:
        if token.owner and token.owner.lower() != ZERO_ADDRESS.lower():
            profile.owner_share = await _share(client, token.address, token.owner, supply)
        if pool_address:
            profile.pool_share = await _share(client, token.address, pool_address, supply)
    return profile


async def _share(client: ChainClient, token: str, holder: str, supply: Decimal) -> Decimal | None:
    try:
        balance = int(await client.call(token, ERC20_ABI, "balanceOf", to_checksum(holder)))
    except Exception as exc:  # noqa: BLE001
        log.debug("balanceOf(%s) для доли: %s", holder, exc)
        return None
    return (Decimal(balance) / supply) * 100
