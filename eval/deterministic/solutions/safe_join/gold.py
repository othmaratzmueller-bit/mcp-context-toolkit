"""GOLD safe_join — realpath + kanonische Containment-Pruefung. Muss alle Achsen gruen."""
import os


def safe_join(base: str, *parts: str) -> str:
    """Join innerhalb von base; ValueError bei jedem Ausbruch/ungueltiger Eingabe."""
    base_real = os.path.realpath(base)
    try:
        target = os.path.realpath(os.path.join(base, *parts))
    except ValueError as exc:  # NUL-Byte o.ae.
        raise ValueError("ungueltiger Pfad") from exc
    try:
        if os.path.commonpath([base_real, target]) != base_real:
            raise ValueError("Ausbruch aus base")
    except ValueError as exc:
        raise ValueError("Ausbruch aus base") from exc
    return target
