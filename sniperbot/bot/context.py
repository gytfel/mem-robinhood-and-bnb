"""Общий контекст, который прокидывается во все хендлеры."""

from __future__ import annotations

from dataclasses import dataclass, field

from sniperbot.access import AccessPolicy
from sniperbot.chain.clients import ChainRegistry
from sniperbot.chain.wallet import WalletService
from sniperbot.config import ChainConfig, Settings
from sniperbot.notify import Notifier
from sniperbot.sniper.engine import SniperEngine
from sniperbot.sniper.executor import Trader
from sniperbot.version import BuildInfo, build_info


@dataclass
class BotContext:
    settings: Settings
    registry: ChainRegistry
    wallets: WalletService
    trader: Trader
    engine: SniperEngine
    notifier: Notifier
    # Сборка, с которой процесс запустился. Файл BUILD на диске обновление
    # переписывает сразу, поэтому читать его при каждом /version нельзя: пока
    # службу не перезапустили, там лежит код, который ещё не работает.
    build: BuildInfo = field(default_factory=build_info)
    # Кто имеет право пользоваться ботом. Объект общий с мидлварью: /access
    # меняет его на ходу, без перезапуска и без правки .env.
    access: AccessPolicy = field(default_factory=AccessPolicy)

    @property
    def active_chain_keys(self) -> list[str]:
        return [key for key, cfg in self.registry.configs.items() if cfg.enabled and cfg.configured]

    def resolve_chain(self, preferred: str | None) -> str:
        """Возвращает рабочую сеть: предпочтительную, иначе первую доступную."""
        keys = self.active_chain_keys
        if preferred and preferred in keys:
            return preferred
        if self.settings.default_chain in keys:
            return self.settings.default_chain
        if keys:
            return keys[0]
        raise RuntimeError("Ни одна сеть не настроена — проверьте config/chains.json и .env")

    def chain(self, key: str) -> ChainConfig:
        return self.registry.config(key)
