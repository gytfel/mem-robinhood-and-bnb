"""Кто имеет право пользоваться ботом.

Право доступа решается в одном месте. Стартовое состояние задаёт `.env`
(`ALLOWED_USER_IDS`), но менять его перезаписью файла с ключами и перезапуском
службы — плохая идея: чаще всего это нужно сделать как раз тогда, когда бот
работает и им уже кто-то пользуется. Поэтому команда `/access` хранит своё
решение в базе, и оно перекрывает файл.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPEN = "open"
PRIVATE = "private"
STATE_MODE = "access_mode"        # ключи в таблице состояния бота
STATE_EXTRA = "access_extra"


@dataclass
class AccessPolicy:
    """Белый список бота: из .env, из команды и всегда — администраторы."""

    admins: frozenset[int] = frozenset()
    env_allowed: frozenset[int] = frozenset()
    extra: set[int] = field(default_factory=set)
    override: str = ""                 # пусто — как задано в .env

    @property
    def mode(self) -> str:
        """Открыт бот всем или только своим."""
        if self.override in {OPEN, PRIVATE}:
            return self.override
        # Пустой ALLOWED_USER_IDS исторически означает «доступ открыт всем».
        return PRIVATE if self.env_allowed else OPEN

    @property
    def is_open(self) -> bool:
        return self.mode == OPEN

    def allows(self, user_id: int) -> bool:
        """Администратор проходит всегда: иначе бота можно закрыть от себя же."""
        if user_id in self.admins:
            return True
        if self.is_open:
            return True
        return user_id in self.env_allowed or user_id in self.extra

    def allowed_ids(self) -> list[int]:
        """Кому открыт доступ в закрытом режиме, кроме администраторов."""
        return sorted(set(self.env_allowed) | set(self.extra))

    def add(self, user_id: int) -> bool:
        """Добавляет в белый список. False — он там уже был."""
        if user_id in self.env_allowed or user_id in self.extra:
            return False
        self.extra.add(user_id)
        return True

    def remove(self, user_id: int) -> bool:
        """Убирает из добавленных командой. Списку из .env это не указ."""
        if user_id not in self.extra:
            return False
        self.extra.discard(user_id)
        return True

    def extra_value(self) -> str:
        """Добавленные ID строкой — так они и лежат в базе."""
        return ",".join(str(uid) for uid in sorted(self.extra))


def parse_ids(raw: str | None) -> set[int]:
    """Разбирает «1,2,3» из базы или из .env."""
    found = set()
    for chunk in (raw or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            found.add(int(chunk))
    return found
