"""wallet — Guthaben in CENT. charge() erwartet CENT und gibt CENT zurück."""
from __future__ import annotations

from .money import Cents


class Wallet:
    def __init__(self, balance_cents: Cents):
        self.balance_cents: Cents = balance_cents

    def charge(self, amount_cents: Cents) -> Cents:
        """Belastet das Guthaben um `amount_cents` (CENT) und gibt den neuen
        Stand in CENT zurück. Wirft ValueError bei unzureichendem Guthaben."""
        if amount_cents > self.balance_cents:
            raise ValueError("insufficient funds")
        self.balance_cents -= amount_cents
        return self.balance_cents
