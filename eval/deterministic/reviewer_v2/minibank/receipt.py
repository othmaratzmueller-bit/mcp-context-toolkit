"""receipt — Quittungszeile für eine Buchung (reine Anzeige)."""
from __future__ import annotations

from .plans import Plan


def receipt_line(plan: Plan) -> str:
    """Formatiert eine Quittungszeile in Euro (Anzeige)."""
    euros = plan.price_cents / 100
    return f"{plan.label}: €{euros:.2f}"
