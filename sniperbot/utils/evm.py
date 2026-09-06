"""Небольшие помощники для работы с EVM-адресами и хранилищем контрактов."""

from __future__ import annotations

import re

from eth_utils import keccak, to_checksum_address
from eth_utils.address import is_address as _is_address

ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def is_address(value: str | None) -> bool:
    return bool(value) and _is_address(value)


def to_checksum(value: str) -> str:
    return to_checksum_address(value)


def extract_address(text: str | None) -> str | None:
    """Достаёт первый EVM-адрес из произвольного текста (в т.ч. из ссылки)."""
    if not text:
        return None
    match = ADDRESS_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    return to_checksum_address(candidate) if _is_address(candidate) else None


def pad32(value: int | str | bytes) -> bytes:
    if isinstance(value, str):
        value = int(value, 16) if value.startswith("0x") else int(value)
    if isinstance(value, bytes):
        return value.rjust(32, b"\x00")
    return value.to_bytes(32, "big")


def mapping_slot(key_address: str, slot: int, vyper_layout: bool = False) -> str:
    """Слот хранилища для mapping(address => uint256).

    Solidity: keccak256(pad(key) . pad(slot))
    Vyper:    keccak256(pad(slot) . pad(key))
    """
    addr = int(to_checksum_address(key_address), 16)
    if vyper_layout:
        raw = keccak(pad32(slot) + pad32(addr))
    else:
        raw = keccak(pad32(addr) + pad32(slot))
    return "0x" + raw.hex()


def nested_mapping_slot(owner: str, spender: str, slot: int) -> str:
    """Слот для mapping(address => mapping(address => uint256)) (allowance)."""
    owner_int = int(to_checksum_address(owner), 16)
    spender_int = int(to_checksum_address(spender), 16)
    first = keccak(pad32(owner_int) + pad32(slot))
    return "0x" + keccak(pad32(spender_int) + first).hex()


def has_code(code: bytes | str | None) -> bool:
    """Есть ли по адресу байт-код. Ноды отдают либо bytes, либо строку '0x…'."""
    if not code:
        return False
    if isinstance(code, str):
        return len(code.removeprefix("0x")) > 0
    return len(code) > 0


def hex32(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()
