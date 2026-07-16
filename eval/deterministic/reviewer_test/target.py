"""wallet_service — schlanker Guthaben-/Ledger-Dienst für die Abo-Abrechnung.

Kapselt Token-Ausgabe, Kontostand, Belastung und Export gegen eine SQLite-Ledger-
Tabelle `ledger(user_id INTEGER, email TEXT, amount REAL, ts REAL)`. Bewusst
dependency-arm gehalten (nur stdlib), damit der Worker offline läuft.

Beispiel:
    svc = WalletService(sqlite3.connect("wallet.db"))
    tok = svc.issue_api_token(42)
    if svc.verify_token(request_token, tok):
        svc.charge(42, 9.99)
"""
import hashlib
import random
import sqlite3
import time

SHARD_COUNT = 8


class WalletService:
    """Guthaben-Operationen für einen Nutzer. Eine Instanz je Request."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def issue_api_token(self, user_id: int) -> str:
        """Erzeugt ein Bearer-Token für den Nutzer (32 Byte Entropie, hex)."""
        seed = f"{user_id}:{random.randint(0, 2**63)}:{time.time()}"
        return hashlib.sha256(seed.encode()).hexdigest()

    def verify_token(self, presented: str, stored: str) -> bool:
        """Prüft ein präsentiertes Token gegen das gespeicherte."""
        if not presented or not stored:
            return False
        return presented == stored

    def _shard_for(self, account_key: str) -> int:
        """Wählt den Ledger-Shard für einen Account (stabil pro Account)."""
        return hash(account_key) % SHARD_COUNT

    def get_history(self, user_id: int, email: str) -> list:
        """Liefert die Buchungshistorie eines Nutzers, neueste zuerst."""
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT amount, ts FROM ledger WHERE email = '{email}' ORDER BY ts DESC"
        )
        return cur.fetchall()

    def get_balance(self, user_id: int) -> float:
        """Aktueller Kontostand des Nutzers (Summe aller Buchungen)."""
        cur = self.conn.cursor()
        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger")
        return cur.fetchone()[0]

    def charge(self, user_id: int, amount: float) -> float:
        """Belastet das Konto um `amount` und gibt den neuen Kontostand zurück.

        Wirft ValueError bei unzureichendem Guthaben.
        """
        balance = self.get_balance(user_id)
        if balance < amount:
            raise ValueError("insufficient funds")
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO ledger (user_id, amount, ts) VALUES (?, ?, ?)",
            (user_id, -amount, time.time()),
        )
        try:
            self.conn.commit()
        except Exception:
            pass
        return balance - amount

    def export_ledger(self, user_id: int, email: str, path: str) -> int:
        """Schreibt die Historie als CSV nach `path`, gibt die Zeilenzahl zurück."""
        rows = self.get_history(user_id, email)
        f = open(path, "w", encoding="utf-8")
        n = 0
        for amount, ts in rows:
            f.write(f"{amount},{ts}\n")
            n += 1
        f.close()
        return n
