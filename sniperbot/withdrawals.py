"""Проверки адреса перед выводом средств.

Перевод необратим, а адрес — двадцать байт без всякого указания на сеть. Отсюда
два самых дорогих способа потерять деньги: отправить в сеть, которой получатель
не знает (биржевой депозит, кошелёк без добавленной сети), и отправить по адресу
с опечаткой. Первое отловить наверняка нельзя — второе можно.

Модуль ничего не знает ни про Telegram, ни про ноду: на вход строка и факты об
адресе, на выход решение. Так правила про чужие деньги читаются целиком.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from eth_utils import is_address, is_checksum_address

from sniperbot.utils.evm import DEAD_ADDRESS, ZERO_ADDRESS

# Адреса других блокчейнов узнаются по форме. Это не полный список сетей, а
# список тех, чьи адреса чаще всего копируют по ошибке.
FOREIGN_CHAINS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(bc1|tb1)[a-z0-9]{25,62}$", re.I), "Bitcoin"),
    (re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$"), "Bitcoin"),
    (re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$"), "Tron"),
    (re.compile(r"^(cosmos|osmo|celestia|inj)1[a-z0-9]{38,}$"), "Cosmos"),
    (re.compile(r"^(EQ|UQ|kQ|0Q)[A-Za-z0-9_\-]{46}$"), "TON"),
    (re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$"), "XRP"),
    (re.compile(r"^X[1-9A-HJ-NP-Za-km-z]{33}$"), "Dash"),
    (re.compile(r"^(ltc1|[LM])[a-km-zA-HJ-NP-Z1-9]{25,62}$"), "Litecoin"),
    (re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$"), "Solana"),
)

BURN_ADDRESSES = {ZERO_ADDRESS.lower(), DEAD_ADDRESS.lower()}


@dataclass(slots=True)
class Destination:
    """Что известно про адрес получателя в этой сети."""

    has_code: bool = False       # по адресу лежит контракт, а не кошелёк
    nonce: int = 0               # сколько транзакций адрес отправлял
    balance: int = 0             # сколько нативной монеты на нём лежит

    @property
    def untouched(self) -> bool:
        """Адрес в этой сети ничего не делал и ничего не имеет."""
        return not self.has_code and self.nonce == 0 and self.balance == 0


def address_problem(text: str) -> str:
    """Почему на этот адрес выводить нельзя. Пустая строка — можно.

    Отклоняем только то, что доказуемо: чужой блокчейн, опечатку по контрольной
    сумме и адрес сжигания. Догадки о том, кому принадлежит адрес, сюда не
    попадают — на них нельзя запрещать чужие деньги.
    """
    value = (text or "").strip()
    if not value:
        return "Пришлите адрес получателя."

    for pattern, chain in FOREIGN_CHAINS:
        if pattern.match(value):
            return (f"Это адрес сети <b>{chain}</b>, а бот работает только с EVM-сетями. "
                    "Монеты, отправленные туда, не дойдут и не вернутся.")

    if not value.startswith("0x") or len(value) != 42 or not is_address(value):
        return "Это не похоже на адрес EVM-кошелька. Нужен формат 0x и 40 символов."

    body = value[2:]
    mixed_case = body != body.lower() and body != body.upper()
    if mixed_case and not is_checksum_address(value):
        # Адрес с заглавными буквами несёт в себе контрольную сумму: если она не
        # сходится, в адресе опечатка, и деньги уйдут в никуда.
        return ("В адресе опечатка: не сходится контрольная сумма. "
                "Скопируйте адрес заново — вручную его набирать нельзя.")

    if value.lower() in BURN_ADDRESSES:
        return "Это адрес сжигания — отправленное туда пропадёт навсегда."

    return ""


def destination_warning(destination: Destination, chain_name: str) -> str:
    """Предупреждение о получателе. Пустая строка — вопросов нет.

    Отличить биржевой депозит от обычного кошелька нельзя: в блокчейне это
    одинаковые адреса. Зато видно то, что их объединяет в опасном случае — в
    этой сети адрес не существовал: биржа его тут не заводила, кошелёк сеть не
    добавлял. Поэтому не запрет, а вопрос.
    """
    if destination.has_code:
        return (f"По этому адресу в сети {chain_name} лежит <b>контракт</b>, а не обычный "
                "кошелёк. Если это ваш мультисиг — всё в порядке; если адрес взят "
                "с биржи или из другой сети, монеты застрянут в нём навсегда.")
    if destination.untouched:
        return (f"В сети <b>{chain_name}</b> этот адрес пуст и не совершал ни одной "
                "операции. Так выглядит и новый кошелёк, и — чаще — адрес с биржи "
                "или из другой сети.\n"
                "Биржа монеты из неизвестной ей сети не зачислит и не вернёт. "
                "Убедитесь, что это ваш кошелёк и что сеть в нём добавлена.")
    return ""
