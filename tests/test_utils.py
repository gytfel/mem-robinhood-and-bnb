from decimal import Decimal

from sniperbot.chain.dex import apply_slippage
from sniperbot.utils.evm import extract_address, mapping_slot, nested_mapping_slot
from sniperbot.utils.fmt import fmt_amount, from_wei, parse_decimal, short_addr, to_wei

DEAD = "0x000000000000000000000000000000000000dEaD"


def test_wei_roundtrip():
    assert to_wei("1.5") == 1_500_000_000_000_000_000
    assert from_wei(1_500_000_000_000_000_000) == Decimal("1.5")
    assert to_wei("1", 6) == 1_000_000


def test_parse_decimal_accepts_comma():
    assert parse_decimal("0,05") == Decimal("0.05")
    assert parse_decimal("  1.25 ") == Decimal("1.25")
    assert parse_decimal("не число") is None
    assert parse_decimal("") is None


def test_fmt_amount_trims_zeros():
    assert fmt_amount(Decimal("1.500000")) == "1.5"
    assert fmt_amount(0) == "0"
    assert fmt_amount(Decimal("12345.6789")) == "12345.67"


def test_extract_address_from_link():
    text = f"смотри https://bscscan.com/token/{DEAD} тут"
    assert extract_address(text) == DEAD
    assert extract_address("нет адреса") is None


def test_short_addr():
    assert short_addr(DEAD) == "0x0000…dEaD"


def test_apply_slippage():
    assert apply_slippage(1000, 1500) == 850
    assert apply_slippage(1000, 0) == 1000
    assert apply_slippage(0, 5000) == 0


def test_mapping_slots_are_deterministic_and_distinct():
    a = mapping_slot(DEAD, 0)
    b = mapping_slot(DEAD, 1)
    assert a == mapping_slot(DEAD, 0)
    assert a != b
    assert mapping_slot(DEAD, 0, vyper_layout=True) != a
    assert len(a) == 66
    assert nested_mapping_slot(DEAD, DEAD, 1) != a


def test_has_code_distinguishes_contracts_from_wallets():
    from sniperbot.utils.evm import has_code

    assert has_code(b"\x60\x80\x60\x40") is True
    assert has_code("0x6080") is True
    assert has_code(b"\x60") is True          # даже крошечный контракт — это контракт
    assert has_code(b"") is False
    assert has_code("0x") is False
    assert has_code(None) is False
