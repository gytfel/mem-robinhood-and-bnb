"""Статические проверки токена: байт-код, прокси, распределение предложения."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sniperbot.chain.clients import ChainClient
from sniperbot.chain.erc20 import TokenInfo
from sniperbot.config import ChainConfig, RouterConfig
from sniperbot.sniper.analysis import (
    EIP1967_IMPLEMENTATION_SLOT,
    ContractProfile,
    is_proxy,
    profile_token,
    scan_bytecode,
    selector,
)

TOKEN = "0x55d398326f99059fF775485246999027B3197955"
OWNER = "0x1111111111111111111111111111111111111111"
POOL = "0x2222222222222222222222222222222222222222"
WNATIVE = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


def code_with(*signatures: str) -> bytes:
    body = "60806040"
    for signature in signatures:
        body += "63" + selector(signature) + "1461"
    return bytes.fromhex(body)


class FakeClient(ChainClient):
    def __init__(self, *, balances=None, storage=None):
        config = ChainConfig(key="test", name="Test", chain_id=1, rpc_urls=["http://localhost"],
                             wrapped_native=WNATIVE,
                             routers=[RouterConfig("D", "0x" + "r" * 40, "0x" + "f" * 40, 25, True)])
        super().__init__(config)
        self.balances = balances or {}
        self.storage = storage or {}

    async def call(self, address, abi, fn_name, *args, **kwargs):
        if fn_name == "balanceOf":
            return self.balances.get(args[0].lower(), 0)
        raise AssertionError(fn_name)

    async def run(self, fn):
        class FakeEth:
            def __init__(self, storage):
                self.storage = storage

            async def get_storage_at(self, address, slot):  # noqa: ANN001
                return bytes.fromhex(self.storage.get(slot, "00" * 32))

        class FakeW3:
            def __init__(self, storage):
                self.eth = FakeEth(storage)

        return await fn(FakeW3(self.storage))


# ------------------------------------------------------------------ байт-код
def test_selectors_match_known_values():
    assert selector("mint(address,uint256)") == "40c10f19"
    assert selector("pause()") == "8456cb59"
    assert selector("transferOwnership(address)") == "f2fde38b"


def test_scan_finds_mint():
    assert scan_bytecode(code_with("mint(address,uint256)")) == {"mint"}


def test_scan_finds_blacklist_variants():
    assert scan_bytecode(code_with("addBotToBlackList(address)")) == {"blacklist"}
    assert scan_bytecode(code_with("setBots(address[],bool)")) == {"blacklist"}


def test_scan_finds_several_powers_at_once():
    found = scan_bytecode(code_with("mint(uint256)", "pause()", "setBuyTax(uint256)"))
    assert found == {"mint", "pause", "fees"}


def test_clean_contract_has_no_powers():
    assert scan_bytecode(code_with("transfer(address,uint256)", "approve(address,uint256)")) == set()


def test_scan_accepts_hex_string_and_empty_code():
    assert scan_bytecode("0x" + code_with("mint(uint256)").hex()) == {"mint"}
    assert scan_bytecode(b"") == set()
    assert scan_bytecode("0x") == set()


# --------------------------------------------------------------------- прокси
async def test_proxy_detected_by_eip1967_slot():
    client = FakeClient(storage={EIP1967_IMPLEMENTATION_SLOT: "00" * 12 + OWNER[2:]})
    assert await is_proxy(client, TOKEN) is True


async def test_plain_contract_is_not_a_proxy():
    assert await is_proxy(FakeClient(), TOKEN) is False


# ------------------------------------------------------------------- профиль
async def test_profile_computes_shares():
    supply = 10**24
    client = FakeClient(balances={OWNER.lower(): supply // 5, POOL.lower(): supply // 2})
    token = TokenInfo(address=TOKEN, symbol="MEME", decimals=18, total_supply=supply, owner=OWNER)

    profile = await profile_token(client, token, code=code_with("mint(address,uint256)"),
                                  pool_address=POOL)

    assert profile.owner_share == pytest.approx(Decimal(20))
    assert profile.pool_share == pytest.approx(Decimal(50))
    assert profile.powers == {"mint"}
    assert profile.owner_can_hurt is True
    assert "чеканка" in profile.describe()


async def test_profile_of_renounced_token_skips_owner_share():
    supply = 10**24
    client = FakeClient(balances={POOL.lower(): supply})
    token = TokenInfo(address=TOKEN, symbol="MEME", decimals=18, total_supply=supply, owner=None)

    profile = await profile_token(client, token, code=code_with(), pool_address=POOL)

    assert profile.owner_share is None
    assert profile.pool_share == pytest.approx(Decimal(100))
    assert profile.owner_can_hurt is False
    assert profile.describe() == "особых прав не нашёл"


def test_empty_profile_is_harmless():
    profile = ContractProfile()
    assert profile.owner_can_hurt is False
    assert profile.owner_share is None
