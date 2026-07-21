"""FOIL safe_join — sauberes commonpath, ABER lexisch (kein realpath). Faengt die
Praefix-Falle korrekt, LAESST aber Symlink-Escape durch (der Symlink base/link ->
base-evil bleibt lexisch 'innerhalb' base). Subtiler Fehler: security < 1.0, aber
knapp — genau der Typ, der einen Stil-Review passiert."""
import os


def safe_join(base: str, *parts: str) -> str:
    """Containment per commonpath, aber ohne Symlink-Aufloesung."""
    base_n = os.path.normpath(os.path.abspath(base))
    target = os.path.normpath(os.path.join(base_n, *parts))
    try:
        if os.path.commonpath([base_n, target]) != base_n:
            raise ValueError("Ausbruch")
    except ValueError as exc:
        raise ValueError("Ausbruch") from exc
    return target
