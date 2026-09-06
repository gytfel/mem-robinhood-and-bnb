"""Загрузка конфигурации сетей и .env."""

from __future__ import annotations

import json

import pytest

from sniperbot.config import Settings, load_chains


@pytest.fixture
def settings():
    return Settings(BOT_TOKEN="token", MASTER_KEY="k" * 32)


def test_bsc_is_configured_out_of_the_box(settings):
    chains = load_chains(settings=settings)
    bsc = chains["bsc"]
    assert bsc.enabled and bsc.configured
    assert bsc.chain_id == 56
    assert bsc.default_router.name == "PancakeSwap V2"
    assert bsc.tx_url("0xabc").endswith("/tx/0xabc")


def test_robinhood_disabled_until_filled(settings):
    chains = load_chains(settings=settings)
    rh = chains["robinhood"]
    assert rh.configured is False
    assert rh.enabled is False


def test_enabled_chains_env_filters(monkeypatch):
    settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32, ENABLED_CHAINS="robinhood")
    chains = load_chains(settings=settings)
    # robinhood не заполнен, поэтому включиться не может, а bsc отфильтрован
    assert chains["bsc"].enabled is False
    assert chains["robinhood"].enabled is False


def test_env_overrides_fill_robinhood(monkeypatch, tmp_path, settings):
    monkeypatch.setenv("RH_RPC_URLS", "https://rpc.example,https://rpc2.example")
    monkeypatch.setenv("RH_CHAIN_ID", "8888")
    monkeypatch.setenv("RH_WRAPPED_NATIVE", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("RH_ROUTER", "0x2222222222222222222222222222222222222222")
    monkeypatch.setenv("RH_FACTORY", "0x3333333333333333333333333333333333333333")
    monkeypatch.setenv("RH_ENABLED", "true")

    chains = load_chains(settings=settings)
    rh = chains["robinhood"]
    assert rh.chain_id == 8888
    assert len(rh.rpc_urls) == 2
    assert rh.configured is True
    assert rh.enabled is True
    assert rh.default_router.router.endswith("2222")


def test_custom_chain_file_is_loaded(tmp_path, settings):
    path = tmp_path / "chains.json"
    path.write_text(json.dumps({
        "base": {
            "name": "Base", "chain_id": 8453, "enabled": True,
            "rpc_urls": ["https://mainnet.base.org"],
            "wrapped_native": "0x4200000000000000000000000000000000000006",
            "routers": [{"name": "Aerodrome", "router": "0xA", "factory": "0xF", "default": True}],
        }
    }), encoding="utf-8")
    chains = load_chains(path, settings=settings)
    assert chains["base"].enabled and chains["base"].configured


def test_runtime_validation_reports_problems():
    problems = Settings(BOT_TOKEN="", MASTER_KEY="short").validate_runtime()
    assert any("BOT_TOKEN" in p for p in problems)
    assert any("MASTER_KEY" in p for p in problems)
    assert Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32).validate_runtime() == []


def test_service_fee_limits():
    with pytest.raises(ValueError):
        Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32, SERVICE_FEE_BPS=9999)
    settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32, SERVICE_FEE_BPS=100)
    assert float(settings.service_fee_rate) == 0.01
    assert any("SERVICE_FEE_WALLET" in p for p in settings.validate_runtime())
