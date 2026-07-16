"""FOIL safe_join — lexisches normpath + str.startswith. Sieht sauber aus, hat aber
ZWEI Loecher: die Praefix-Falle (base vs base-evil, String-Prefix) UND Symlink-Escape
(kein realpath). Soll auf der security-Achse rot werden, correctness gruen."""
import os


def safe_join(base: str, *parts: str) -> str:
    """Naive Variante: rein lexisch, String-Vergleich."""
    base_n = os.path.normpath(os.path.abspath(base))
    target = os.path.normpath(os.path.join(base_n, *parts))
    if not target.startswith(base_n):
        raise ValueError("Ausbruch")
    return target
