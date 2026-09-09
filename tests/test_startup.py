"""Уведомление о перезапуске: определение обновления и аварийного завершения."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from sniperbot.bot.startup import RestartReport, human_duration, record_start, record_stop, render_restart
from sniperbot.db.base import session_scope
from sniperbot.db.models import BotRun
from sniperbot.version import UNKNOWN, BuildInfo, _from_env, _from_file, write_build_file


def build(commit: str = "abc12345") -> BuildInfo:
    return BuildInfo(commit=commit, date="07.09 06:40", branch="main", source="git")


# --------------------------------------------------------------- сведения о сборке
def test_same_code_detection():
    info = build("abc12345")
    assert info.same_code_as("abc12345") is True
    assert info.same_code_as("def67890") is False
    assert info.same_code_as(None) is False
    assert info.same_code_as(UNKNOWN) is False
    # неизвестная сборка ни с чем не совпадает — лучше лишний раз сказать «обновился»
    assert BuildInfo().same_code_as("abc12345") is False


def test_build_file_roundtrip(tmp_path):
    path = tmp_path / "BUILD"
    write_build_file(build("cafe1234"), path)
    assert json.loads(path.read_text(encoding="utf-8"))["commit"] == "cafe1234"

    loaded = _from_file(path)
    assert loaded.commit == "cafe1234"
    assert loaded.source == "file"


def test_missing_build_file_is_not_an_error(tmp_path):
    assert _from_file(tmp_path / "нет") is None


def test_broken_build_file_is_ignored(tmp_path):
    path = tmp_path / "BUILD"
    path.write_text("не json", encoding="utf-8")
    assert _from_file(path) is None


def test_env_overrides_everything(monkeypatch):
    monkeypatch.setenv("SNIPER_BUILD_COMMIT", "deadbeef99")
    monkeypatch.setenv("SNIPER_BUILD_DATE", "01.01 00:00")
    info = _from_env()
    assert info.commit == "deadbeef99"[:12]
    assert info.source == "env"


def test_short_description_survives_unknown_build():
    assert BuildInfo().short() == "v1.0.0"
    assert "коммит" in build().short()


# --------------------------------------------------------------------- история
async def test_first_start_is_marked(db):
    report = await record_start(build())
    assert report.first_start is True
    assert report.updated is False
    assert "впервые" in render_restart(report)


async def test_restart_without_changes(db):
    first = await record_start(build("aaa11111"))
    await record_stop(first.run_id)
    second = await record_start(build("aaa11111"))

    assert second.first_start is False
    assert second.updated is False
    assert second.unclean is False
    text = render_restart(second)
    assert "код не менялся" in text
    assert "аварийно" not in text


async def test_update_is_detected_and_shown(db):
    first = await record_start(build("aaa11111"))
    await record_stop(first.run_id)
    second = await record_start(build("bbb22222"))

    assert second.updated is True
    text = render_restart(second)
    assert "обновлён" in text
    assert "aaa11111" in text and "bbb22222" in text


async def test_crash_is_detected(db):
    await record_start(build("aaa11111"))      # штатной остановки не было
    second = await record_start(build("aaa11111"))

    assert second.unclean is True
    assert "аварийно" in render_restart(second)


async def test_downtime_and_uptime_are_reported(db):
    async with session_scope() as session:
        session.add(BotRun(
            started_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=5),
            stopped_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3),
            version="1.0.0", commit="aaa11111", clean_shutdown=True,
        ))

    report = await record_start(build("aaa11111"))
    assert report.previous_uptime is not None
    assert 4.9 < report.previous_uptime.total_seconds() / 3600 < 5.1
    assert report.downtime.total_seconds() < 300

    text = render_restart(report)
    assert "простой" in text
    assert "проработал" in text


async def test_stop_marks_the_run_clean(db):
    report = await record_start(build())
    await record_stop(report.run_id)

    async with session_scope() as session:
        run = await session.get(BotRun, report.run_id)
    assert run.clean_shutdown is True
    assert run.stopped_at is not None


async def test_stop_without_run_id_is_safe(db):
    await record_stop(None)      # не должно падать


# ----------------------------------------------------------------- оформление
def test_stats_appear_in_the_message():
    report = RestartReport(info=build(), first_start=True)
    report.stats = {"chains": ["BNB Smart Chain ✅ блок 42"], "positions": 3,
                    "snipers": 1, "users": 2}
    text = render_restart(report)
    assert "BNB Smart Chain ✅ блок 42" in text
    assert "открытых позиций: 3" in text
    assert "автоснайп включён у 1" in text


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (dt.timedelta(seconds=42), "42 с"),
        (dt.timedelta(minutes=7), "7 мин"),
        (dt.timedelta(hours=5, minutes=12), "5 ч 12 мин"),
        (dt.timedelta(days=2, hours=3), "2 д 3 ч"),
        (None, "—"),
    ],
)
def test_human_duration(delta, expected):
    assert human_duration(delta) == expected


# --------------------------------------------- расхождение диска и процесса
def test_stale_build_warning_fires_when_disk_is_newer():
    """Файл BUILD обновление переписывает сразу — /version не должен на это вестись."""
    from sniperbot.bot.startup import stale_build_warning

    running = BuildInfo(commit="aaaa1111", date="01.09 10:00")
    on_disk = BuildInfo(commit="bbbb2222", date="09.09 20:10")
    warning = stale_build_warning(running, on_disk)

    assert "старом коде" in warning
    assert "bbbb2222" in warning       # видно, какая сборка ждёт перезапуска
    assert "restart" in warning


def test_no_warning_when_running_code_matches_disk():
    from sniperbot.bot.startup import stale_build_warning

    info = BuildInfo(commit="aaaa1111")
    assert stale_build_warning(info, info) == ""
    assert stale_build_warning(info, BuildInfo(commit=UNKNOWN)) == ""


def test_warning_when_build_file_appeared_after_start():
    """До обновления файла BUILD не было — процесс заведомо старее."""
    from sniperbot.bot.startup import stale_build_warning

    warning = stale_build_warning(BuildInfo(), BuildInfo(commit="bbbb2222"))
    assert "старом коде" in warning
