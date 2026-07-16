"""money — die eine Geld-Invariante von minibank.

INVARIANTE (projektweit, nicht verhandelbar):
    Alle Geldbeträge sind ganzzahlige CENT (int). Niemals float, niemals Euro
    in Speicherung oder Rechnung. Euro existiert AUSSCHLIESSLICH als Anzeige-
    Formatierung am äußersten Rand (format_eur). Wer einen Cent-Betrag durch 100
    teilt, verlässt die Invariante — das ist nur für die Darstellung erlaubt.
"""
from __future__ import annotations

Cents = int


def format_eur(amount_cents: Cents) -> str:
    """Formatiert einen Cent-Betrag als Euro-String — NUR für die Anzeige."""
    return f"€{amount_cents / 100:.2f}"


def add(*amounts_cents: Cents) -> Cents:
    """Summiert Cent-Beträge (bleibt int)."""
    return sum(amounts_cents)


def apply_percent(amount_cents: Cents, percent: int) -> Cents:
    """Wendet einen ganzzahligen Prozentsatz an und rundet auf ganze Cent.

    Bleibt innerhalb der Cent-Invariante: das Zwischenprodukt ist float, das
    Ergebnis wird sofort auf int-Cent gerundet.
    """
    return round(amount_cents * percent / 100)
