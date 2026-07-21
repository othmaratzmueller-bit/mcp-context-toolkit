"""Gold-Referenz (Opus 4.8) fuer die safe_join-Aufgabe — delegiert erzeugt 2026-07-12.
Ansatz: os.path.realpath + os.path.commonpath (Containment auf Pfad-Komponenten).

Bedachte Edge-Cases (Opus):
- Symlink-Grenze: realpath loest Symlinks in base UND Ergebnis auf; ein Symlink in
  base, der nach aussen zeigt, wird gefangen (lexikalisches normpath nicht).
- Praefix-Falle /base vs /base-evil: commonpath auf Komponenten statt startswith;
  Windows "verschiedene Laufwerke" (commonpath wirft ValueError) = ausserhalb.
- Absolute parts + relatives base: absolute Segmente explizit abgewiesen (join wuerde
  sonst reset-en); relatives base gegen CWD aufgeloest; safe_join(base) ohne parts ok.
"""
from __future__ import annotations

import os

# Path segments that carry no information and are silently dropped.
_IGNORED_SEGMENTS = frozenset({"", os.curdir})  # "" and "."


def safe_join(base: str, *parts: str) -> str:
    """Join ``base`` with ``parts`` and return the normalized ABSOLUTE path,
    guaranteed to stay inside ``base``.

    Empty parts and ``"."`` are ignored; ``".."`` staying inside ``base`` is
    allowed (``"a/../b"`` -> ``<base>/b``). Raises ``ValueError`` if a part is
    absolute or if the combined path (``..`` or symlinks) resolves outside
    ``base``.
    """
    base_real = os.path.realpath(base)

    safe_parts: list[str] = []
    for part in parts:
        if part in _IGNORED_SEGMENTS:
            continue
        if os.path.isabs(part):
            raise ValueError(
                f"safe_join: absolute path segment is not allowed: {part!r}"
            )
        safe_parts.append(part)

    candidate = os.path.realpath(os.path.join(base_real, *safe_parts))

    try:
        inside = os.path.commonpath([base_real, candidate]) == base_real
    except ValueError:
        inside = False

    if not inside:
        raise ValueError(
            f"safe_join: path escapes base — parts {parts!r} on {base!r} "
            f"resolve to {candidate!r}, outside {base_real!r}"
        )

    return candidate
