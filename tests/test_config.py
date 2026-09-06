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


def test_relative_sqlite_path_anchored_to_env_dir(monkeypatch, tmp_path):
    """Относительный путь к базе не должен зависеть от текущего каталога."""
    env_file = tmp_path / "server" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("SNIPER_ENV_FILE", str(env_file))

    settings = Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32,
                        DATABASE_URL="sqlite+aiosqlite:///data/sniper.db")
    assert settings.resolved_database_url == (
        f"sqlite+aiosqlite:///{tmp_path / 'server' / 'data' / 'sniper.db'}"
    )


def test_absolute_and_memory_database_urls_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("SNIPER_ENV_FILE", str(tmp_path / ".env"))
    absolute = "sqlite+aiosqlite:////var/lib/sniper.db"
    assert Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32,
                    DATABASE_URL=absolute).resolved_database_url == absolute

    memory = "sqlite+aiosqlite:///:memory:"
    assert Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32,
                    DATABASE_URL=memory).resolved_database_url == memory

    postgres = "postgresql+asyncpg://user:pass@localhost/sniper"
    assert Settings(BOT_TOKEN="t", MASTER_KEY="k" * 32,
                    DATABASE_URL=postgres).resolved_database_url == postgres


def test_missing_lists_empty_fields(settings):
    chains = load_chains(settings=settings)
    rh = chains["robinhood"]
    # RPC и chain_id уже заполнены в конфиге, адресов DEX ещё нет
    assert "WRAPPED_NATIVE" in rh.missing
    assert any("ROUTER" in gap for gap in rh.missing)
    assert "RPC_URLS" not in rh.missing
    assert chains["bsc"].missing == []


def test_chain_without_chain_id_is_not_configured(tmp_path, settings):
    import json

    path = tmp_path / "chains.json"
    path.write_text(json.dumps({
        "x": {"name": "X", "chain_id": 0, "enabled": True,
              "rpc_urls": ["https://rpc"], "wrapped_native": "0x" + "b" * 40,
              "routers": [{"name": "D", "router": "0x" + "r" * 40,
                           "factory": "0x" + "f" * 40, "default": True}]}
    }), encoding="utf-8")
    chain = load_chains(path, settings=settings)["x"]
    assert chain.missing == ["CHAIN_ID"]
    assert chain.configured is False
    assert chain.enabled is False  # ненастроенную сеть бот не запускает


def test_chain_overrides_are_read_from_env_file(tmp_path, monkeypatch):
    """RH_* из .env должны применяться без export в окружение (регресс)."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BOT_TOKEN=t\n"
        "MASTER_KEY=" + "k" * 32 + "\n"
        "ENABLED_CHAINS=bsc, robinhood\n"
        "RH_ENABLED=true\n"
        "RH_CHAIN_ID=4663\n"
        "RH_RPC_URLS=https://rpc.mainnet.chain.robinhood.com\n"
        "RH_ROUTER=0x89e5db8b5aa49aa85ac63f691524311aeb649eba\n"
        "RH_FACTORY=0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f\n"
        "RH_WRAPPED_NATIVE=0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SNIPER_ENV_FILE", str(env_file))
    for name in ("RH_ENABLED", "RH_ROUTER", "RH_FACTORY", "RH_WRAPPED_NATIVE", "RH_RPC_URLS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    rh = load_chains(settings=settings)["robinhood"]

    assert rh.missing == []
    assert rh.configured is True
    assert rh.enabled is True
    assert rh.default_router.router.endswith("649eba")
    assert rh.wrapped_native.endswith("EAcAD73")


def test_process_env_beats_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("RH_ROUTER=0x" + "1" * 40 + "\n", encoding="utf-8")
    monkeypatch.setenv("SNIPER_ENV_FILE", str(env_file))
    monkeypatch.setenv("RH_ROUTER", "0x" + "2" * 40)

    from sniperbot.config import env_values

    assert env_values()["RH_ROUTER"] == "0x" + "2" * 40


def test_v3_router_from_env(monkeypatch, tmp_path, settings):
    monkeypatch.setenv("RH_V3_ROUTER", "0x" + "a" * 40)
    monkeypatch.setenv("RH_V3_FACTORY", "0x" + "b" * 40)
    monkeypatch.setenv("RH_V3_QUOTER", "0x" + "c" * 40)
    monkeypatch.setenv("RH_V3_FEES", "100,500,2500,10000")

    chains = load_chains(settings=settings)
    v3 = [r for r in chains["robinhood"].routers if r.kind == "v3"]
    assert len(v3) == 1
    assert v3[0].configured is True
    assert v3[0].fee_tiers == (100, 500, 2500, 10000)
    assert v3[0].is_v3 is True


def test_v3_without_quoter_is_not_configured(monkeypatch, settings):
    monkeypatch.setenv("RH_V3_ROUTER", "0x" + "a" * 40)
    monkeypatch.setenv("RH_V3_FACTORY", "0x" + "b" * 40)
    monkeypatch.delenv("RH_V3_QUOTER", raising=False)

    v3 = [r for r in load_chains(settings=settings)["robinhood"].routers
          if r.kind == "v3" and r.router][0]
    assert v3.configured is False
    assert v3.missing == ["QUOTER"]


def test_default_dex_switches_primary_venue(monkeypatch, settings):
    monkeypatch.setenv("BSC_V3_ROUTER", "0x" + "a" * 40)
    monkeypatch.setenv("BSC_V3_FACTORY", "0x" + "b" * 40)
    monkeypatch.setenv("BSC_V3_QUOTER", "0x" + "c" * 40)
    monkeypatch.setenv("BSC_DEFAULT_DEX", "v3")

    bsc = load_chains(settings=settings)["bsc"]
    assert bsc.default_router.kind == "v3"
    # обе площадки остаются активными: сканер слушает и V2, и V3
    assert {r.kind for r in bsc.active_routers} == {"v2", "v3"}
