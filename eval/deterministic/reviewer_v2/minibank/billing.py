"""billing — Abo-Abrechnung. Neues Feature auf Basis von wallet + plans + money."""
from __future__ import annotations

from .money import format_eur
from .plans import get_plan
from .wallet import Wallet


def charge_subscription(wallet: Wallet, plan_key: str) -> str:
    """Bucht die Monatsgebühr des gewählten Plans vom Wallet ab und gibt eine
    Bestätigungszeile zurück."""
    plan = get_plan(plan_key)
    new_balance = wallet.charge(plan.price_cents / 100)
    return f"{plan.label} gebucht — neuer Stand: {format_eur(new_balance)}"
