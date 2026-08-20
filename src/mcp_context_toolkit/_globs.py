"""Glob-Pattern-Compiler — STDLIB-PURE, keine Package-Imports.

Ausgelagert aus `engine.py` (Reviewer-Fund, Handbuch-Stufe-1-Sprint
2026-08-16): `generate_handbuch.py` unter `tools/code-graph/` matcht
Decision-Karten (`.claude/work/decisions/*.md::evidence.files`, bis Etappe C
2026-08-19: `.claude/decisions/*.yaml::applies_to.files`) gegen Datei-Pfade
mit exakt derselben Glob-Semantik wie diese Engine (`applies_to.files` bei
Rules) —
vorher pflegte `generate_handbuch.py` eine woertliche Kopie dieser Funktion
(S8-Verstoss: zwei Quellen fuer denselben Algorithmus, die auseinanderlaufen
koennen). Diese Datei ist jetzt die EINE Quelle fuer beide.

WARUM eine eigene Datei statt `engine.py` direkt zu importieren: ein Import
von `mcp_context_toolkit` (auch nur eines Untermoduls, z.B.
`mcp_context_toolkit.engine`) fuehrt IMMER zuerst `__init__.py` des Pakets
aus (Python-Paket-Semantik) — und die zieht `RulesEngine` -> `models.py` ->
`pydantic`. `tools/code-graph/` ist bewusst zero-dependency (siehe
`combine.py`-Kopfkommentar: "KEINE externen Pakete") und laeuft im
System-Python OHNE das Engine-venv, in dem pydantic fehlt. Der Verbraucher
laedt DIESE Datei deshalb direkt per Pfad (`importlib.util.
spec_from_file_location`), am `mcp_context_toolkit`-Package-`__init__.py`
vorbei — das funktioniert nur, WEIL diese Datei (und nur diese) auf jeden
Package-Import verzichtet.

PFLICHT bei jeder Aenderung hier: NUR `re`/`functools`/Sprachkern — kein
`import mcp_context_toolkit...`, kein drittes Paket. Ein einziger `import
yaml` oder `import pydantic` hier wuerde den Datei-Load in
`generate_handbuch.py` sofort brechen.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Cache-Groesse: die zwei Verbraucher haben unterschiedliche Lastprofile
# (Rules-Engine: wenige Dutzend Rule-Globs; generate_handbuch.py: bis zu
# einige hundert distinkte Decisions-Globs aus .claude/decisions/*.yaml,
# gegen ~1197 Datei-Pfade gematcht). Der groessere Wert ist fuer BEIDE sicher
# -- ein ueberdimensionierter LRU-Cache kostet nur etwas Speicher, nie
# Korrektheit; ein zu kleiner erzwingt bei der Handbuch-Seite staendiges
# Neukompilieren. Deshalb ein gemeinsamer fixer Wert (der groessere der
# beiden vorherigen: 512 in engine.py, 2048 in generate_handbuch.py) statt
# eines Parameters, den ohnehin niemand unterschiedlich setzen wuerde.
_GLOB_CACHE_SIZE = 2048


@lru_cache(maxsize=_GLOB_CACHE_SIZE)
def compile_glob(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern with '**' support to a regex.

    Semantics match pathlib PurePosixPath.match for '**' recursive matching
    and '*' single-segment matching. '?' matches a single non-'/' character.
    """
    i = 0
    n = len(pattern)
    out: list[str] = ["^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c in ".+()|^$[]{}\\":
            out.append("\\" + c)
        else:
            out.append(c)
        i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(path: str, pattern: str) -> bool:
    """True, wenn `path` das Glob-Pattern `pattern` erfuellt."""
    return compile_glob(pattern).match(path) is not None
