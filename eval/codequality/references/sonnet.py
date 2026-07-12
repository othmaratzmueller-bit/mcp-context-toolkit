"""Gold-Referenz (Sonnet 5) fuer die safe_join-Aufgabe — delegiert erzeugt 2026-07-12.
Ansatz: pathlib.Path.resolve() + Path.relative_to (Containment auf Komponenten).

Bedachte Edge-Cases (Sonnet):
- Symlink-Grenze: resolve() am fertig zusammengesetzten candidate (nicht nur lexikalisch);
  Symlink in base der rausfuehrt wird beim relative_to-Check erkannt.
- Absolute parts: os.path.isabs pro Part VOR dem Anhaengen — sonst ersetzt Path('/base')/'/x'
  wegen pathlib-Semantik still alles Vorherige (-> /x).
- Relatives base + Trailing-Slash-Falle: base zuerst selbst resolve(); Containment via
  relative_to() statt startswith() (/base-evil sonst faelschlich "in /base").
"""
import os
from pathlib import Path


def safe_join(base: str, *parts: str) -> str:
    """Join `base` with `parts` into a normalized absolute path that is
    guaranteed to stay inside `base`.

    Raises ValueError if any part is an absolute path, or if the combined,
    fully-resolved path (symlinks included) would fall outside `base`.
    Empty parts and '.' are ignored; '..' segments that stay within `base`
    are allowed.
    """
    base_path = Path(base).resolve()

    candidate = base_path
    for part in parts:
        if not part or part == os.curdir:
            continue
        if os.path.isabs(part):
            raise ValueError(
                f"safe_join: part {part!r} is an absolute path and is not "
                f"allowed inside base {str(base_path)!r}"
            )
        candidate = candidate / part

    resolved = candidate.resolve()

    try:
        resolved.relative_to(base_path)
    except ValueError:
        raise ValueError(
            f"safe_join: {str(resolved)!r} escapes base {str(base_path)!r}"
        ) from None

    return str(resolved)
