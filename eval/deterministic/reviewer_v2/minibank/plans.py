"""plans — Abo-Katalog. Preise in CENT (money-Invariante)."""
from __future__ import annotations

from dataclasses import dataclass

from .money import Cents


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    price_cents: Cents   # z.B. 999 == €9,99


PLANS: dict[str, Plan] = {
    "basic": Plan("basic", "Basic", 999),
    "pro": Plan("pro", "Pro", 2499),
    "team": Plan("team", "Team", 9900),
}


def get_plan(key: str) -> Plan:
    return PLANS[key]
