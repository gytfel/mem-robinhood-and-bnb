"""CLI: создание .env, разбор аргументов, вспомогательные проверки."""

from __future__ import annotations

import os
import stat

import pytest

from sniperbot import cli


def test_set_env_line_replaces_and_appends():
    text = "# коммент\nBOT_TOKEN=old\nLOG_LEVEL=INFO\n"
    updated = cli.set_env_line(text, "BOT_TOKEN", "new")
    assert "BOT_TOKEN=new" in updated
    assert "BOT_TOKEN=old" not in updated
    assert "# коммент" in updated  # комментарии сохраняются

    with_new_key = cli.set_env_line(updated, "MASTER_KEY", "abc")
    assert with_new_key.endswith("MASTER_KEY=abc\n")


def test_set_env_line_touches_only_exact_key():
    text = "RH_ROUTER=0x1\nROUTER=0x2\n"
    updated = cli.set_env_line(text, "ROUTER", "0x9")
    assert "RH_ROUTER=0x1" in updated
    assert "ROUTER=0x9" in updated


def test_generated_master_key_is_long_and_unique():
    first, second = cli.generate_master_key(), cli.generate_master_key()
    assert len(first) >= 48
    assert first != second


def test_mask_hides_middle():
    masked = cli.mask("7123456789:AAF-secret-part-here")
    assert masked.startswith("71234567")
    assert "secret" not in masked


def test_json_or_empty():
    assert cli._json_or_empty('{"ok": true}') == {"ok": True}
    assert cli._json_or_empty("не json") == {}
    assert cli._json_or_empty("[1, 2]") == {}


def test_check_database_reports_writable_dir(tmp_path):
    ok, note = cli._check_database(f"sqlite+aiosqlite:///{tmp_path}/data/sniper.db")
    assert ok is True
    assert "доступен на запись" in note


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root игнорирует права доступа")
def test_check_database_detects_unwritable_dir(tmp_path):
    blocked = tmp_path / "readonly"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        ok, note = cli._check_database(f"sqlite+aiosqlite:///{blocked}/sub/sniper.db")
        assert ok is False
        assert "недоступен" in note
    finally:
        blocked.chmod(0o700)


def test_init_writes_env_with_secure_permissions(tmp_path, capsys):
    env_path = tmp_path / ".env"
    code = cli.main([
        "--env-file", str(env_path), "init", "--yes",
        "--bot-token", "7123456789:AAF-" + "x" * 32,
        "--admin-id", "42", "--private",
    ])
    assert code == 0

    content = env_path.read_text(encoding="utf-8")
    assert "BOT_TOKEN=7123456789:AAF-" in content
    assert "ADMIN_IDS=42" in content
    assert "ALLOWED_USER_IDS=42" in content

    master_key = next(line for line in content.splitlines() if line.startswith("MASTER_KEY="))
    assert len(master_key.split("=", 1)[1]) >= 48

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600  # ключи не должны читаться другими пользователями

    assert "MASTER_KEY" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BOT_TOKEN=existing\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env_path), "init", "--yes", "--bot-token", "x" * 40])
    assert env_path.read_text(encoding="utf-8") == "BOT_TOKEN=existing\n"


def test_init_force_overwrites(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BOT_TOKEN=existing\n", encoding="utf-8")
    code = cli.main([
        "--env-file", str(env_path), "init", "--yes", "--force",
        "--bot-token", "7000000000:AAF-" + "y" * 32,
    ])
    assert code == 0
    assert "existing" not in env_path.read_text(encoding="utf-8")


def test_init_requires_bot_token(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(tmp_path / ".env"), "init", "--yes"])


def test_parser_exposes_all_commands():
    parser = cli.build_parser()
    for command in ("init", "doctor", "run", "check", "wallets", "keygen"):
        args = parser.parse_args([command] + (["0xabc"] if command == "check" else []))
        assert callable(args.func)


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    assert "sniper init" in capsys.readouterr().out


def test_env_file_flag_is_exported(tmp_path):
    """--env-file должен попасть в окружение до чтения конфигурации."""
    env_path = tmp_path / "custom.env"
    cli.main(["--env-file", str(env_path), "init", "--yes", "--bot-token", "7000000000:AAF-" + "z" * 32])
    assert os.environ["SNIPER_ENV_FILE"] == str(env_path)


# --------------------------------------------------------------- sniper discover
class _StubClient:
    """Роутер, отвечающий заранее заданными адресами."""

    def __init__(self, *, code=b"\x60\x60", weth="0x" + "b" * 40,
                 factory="0x" + "f" * 40, pairs=1234, fail_on=None):
        self._code = code
        self._weth = weth
        self._factory = factory
        self._pairs = pairs
        self._fail_on = fail_on

    async def run(self, fn):
        return self._code

    async def call(self, address, abi, fn_name, *args, **kwargs):
        if fn_name == self._fail_on:
            raise ValueError("execution reverted")
        return {"WETH": self._weth, "factory": self._factory, "allPairsLength": self._pairs}[fn_name]


async def test_probe_router_returns_factory_and_weth():
    found = await cli.probe_router(_StubClient(), "0x" + "r" * 40)
    assert found["factory"] == "0x" + "f" * 40
    assert found["weth"] == "0x" + "b" * 40
    assert found["pairs"] == 1234


async def test_probe_router_rejects_address_without_code():
    with pytest.raises(ValueError, match="нет кода"):
        await cli.probe_router(_StubClient(code=b""), "0x" + "r" * 40)


async def test_probe_router_rejects_non_v2_router():
    """У Universal Router (v3/v4) нет WETH() — такой адрес не должен пройти."""
    with pytest.raises(ValueError):
        await cli.probe_router(_StubClient(fail_on="WETH"), "0x" + "r" * 40)


async def test_probe_router_rejects_wrong_factory():
    with pytest.raises(ValueError):
        await cli.probe_router(_StubClient(fail_on="allPairsLength"), "0x" + "r" * 40)


def test_discover_rejects_garbage_address(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BOT_TOKEN=t\nMASTER_KEY=" + "k" * 32 + "\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        cli.main(["--env-file", str(env_path), "discover", "не-адрес"])
