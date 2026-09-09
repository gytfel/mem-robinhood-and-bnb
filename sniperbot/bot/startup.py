"""Учёт запусков бота и сообщение о перезапуске.

Задача — по одному сообщению в Telegram понимать: бот действительно обновился
или просто перезапустился; штатно ли завершился прошлый запуск и сколько он
проработал.
"""

from __future__ import annotations

import datetime as dt
import logging
import socket
from dataclasses import dataclass, field

from sqlalchemy import select

from sniperbot.db.base import session_scope
from sniperbot.db.models import BotRun, utcnow
from sniperbot.utils.fmt import esc
from sniperbot.version import BuildInfo, build_info

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RestartReport:
    info: BuildInfo
    run_id: int | None = None
    previous_commit: str = ""
    previous_version: str = ""
    updated: bool = False
    first_start: bool = False
    unclean: bool = False
    downtime: dt.timedelta | None = None
    previous_uptime: dt.timedelta | None = None
    stats: dict = field(default_factory=dict)


def stale_build_warning(running: BuildInfo, on_disk: BuildInfo) -> str:
    """Предупреждение, если на диске лежит сборка новее работающей.

    Обновление переписывает файл BUILD сразу, а код в памяти процесса меняется
    только при перезапуске службы. Расхождение — это ровно случай «обновился,
    а новых команд нет»: пока об этом не сказать прямо, /version выглядит так,
    будто всё хорошо.
    """
    if not on_disk.known or running.same_code_as(on_disk):
        return ""
    when = f" от {esc(on_disk.date)}" if on_disk.date else ""
    return (
        "⚠️ <b>Бот работает на старом коде.</b>\n"
        f"На диске уже сборка <code>{esc(on_disk.commit)}</code>{when}, "
        "но службу после обновления не перезапускали.\n"
        "Перезапуск: <code>systemctl restart memecoin-sniper</code>\n"
        "Не помогло — причину покажет <code>bash scripts/diagnose.sh</code>"
    )


def human_duration(delta: dt.timedelta | None) -> str:
    """«5 ч 12 мин», «42 с» — коротко и по-русски."""
    if delta is None:
        return "—"
    seconds = int(max(0, delta.total_seconds()))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes and not days:
        parts.append(f"{minutes} мин")
    if not parts:
        parts.append(f"{seconds} с")
    return " ".join(parts)


async def record_start(info: BuildInfo | None = None) -> RestartReport:
    """Пишет новый запуск в историю и сравнивает его с предыдущим."""
    info = info or build_info()
    now = utcnow()

    async with session_scope() as session:
        previous = await session.scalar(select(BotRun).order_by(BotRun.id.desc()).limit(1))
        report = RestartReport(info=info, first_start=previous is None)

        if previous is not None:
            report.previous_commit = previous.commit or ""
            report.previous_version = previous.version or ""
            report.updated = not info.same_code_as(previous.commit) and bool(previous.commit)
            report.unclean = not previous.clean_shutdown
            last_seen = previous.stopped_at or previous.started_at
            report.downtime = now - _aware(last_seen)
            if previous.stopped_at:
                report.previous_uptime = _aware(previous.stopped_at) - _aware(previous.started_at)

        run = BotRun(
            started_at=now, version=info.version, commit=info.commit,
            branch=info.branch, build_date=info.date, clean_shutdown=False,
            host=socket.gethostname()[:64],
        )
        session.add(run)
        await session.flush()
        report.run_id = run.id
    return report


async def record_stop(run_id: int | None) -> None:
    """Отмечает штатное завершение — по нему видно, был ли следующий старт аварийным."""
    if run_id is None:
        return
    try:
        async with session_scope() as session:
            run = await session.get(BotRun, run_id)
            if run is not None:
                run.stopped_at = utcnow()
                run.clean_shutdown = True
    except Exception as exc:  # noqa: BLE001 - на выходе это не повод падать
        log.debug("Не смог отметить штатную остановку: %s", exc)


async def collect_stats(registry, chain_keys: list[str]) -> dict:  # noqa: ANN001
    """Короткая сводка состояния для сообщения о перезапуске."""
    from sqlalchemy import func

    from sniperbot.db.models import ChainSettings, Position, User

    stats: dict = {"chains": []}
    for key in chain_keys:
        config = registry.config(key)
        try:
            block = await registry.get(key).block_number()
            stats["chains"].append(f"{config.name} ✅ блок {block}")
        except Exception as exc:  # noqa: BLE001 - нода могла лечь, это часть отчёта
            log.warning("Сеть %s недоступна при старте: %s", key, exc)
            stats["chains"].append(f"{config.name} ⛔️ нет связи")

    try:
        async with session_scope() as session:
            stats["users"] = int(await session.scalar(select(func.count()).select_from(User)) or 0)
            stats["positions"] = int(await session.scalar(
                select(func.count()).select_from(Position).where(Position.status == "open")) or 0)
            stats["snipers"] = int(await session.scalar(
                select(func.count(func.distinct(ChainSettings.user_id)))
                .where(ChainSettings.auto_snipe.is_(True))) or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("Статистика для уведомления недоступна: %s", exc)
    return stats


def render_restart(report: RestartReport) -> str:
    """Текст уведомления о перезапуске."""
    info = report.info

    if report.first_start:
        title = "🚀 <b>Бот запущен впервые</b>"
    elif report.updated:
        title = "🆕 <b>Бот обновлён и перезапущен</b>"
    else:
        title = "♻️ <b>Бот перезапущен</b> (код не менялся)"

    lines = [title, esc(info.short())]
    if info.branch:
        lines.append(f"Ветка: <code>{esc(info.branch)}</code>")

    if report.updated and report.previous_commit:
        lines.append(f"Код: <code>{esc(report.previous_commit)}</code> → "
                     f"<code>{esc(info.commit)}</code>")
    elif report.updated and report.previous_version:
        lines.append(f"Версия: {esc(report.previous_version)} → {esc(info.version)}")

    if not report.first_start:
        if report.unclean:
            lines.append("⚠️ Предыдущий запуск завершился аварийно "
                         "(падение, перезагрузка или kill -9)")
        details = [f"простой {human_duration(report.downtime)}"]
        if report.previous_uptime is not None:
            details.append(f"прошлый запуск проработал {human_duration(report.previous_uptime)}")
        lines.append("⏱ " + " · ".join(details))

    stats = report.stats
    if stats.get("chains"):
        lines.append("\n🌐 " + " · ".join(stats["chains"]))
    summary = []
    if "positions" in stats:
        summary.append(f"открытых позиций: {stats['positions']}")
    if "snipers" in stats:
        summary.append(f"автоснайп включён у {stats['snipers']}")
    if "users" in stats:
        summary.append(f"пользователей: {stats['users']}")
    if summary:
        lines.append("📊 " + " · ".join(summary))

    if report.updated:
        lines.append("\nЧто нового — в описании коммита; настройки и позиции сохранены.")
    return "\n".join(lines)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
