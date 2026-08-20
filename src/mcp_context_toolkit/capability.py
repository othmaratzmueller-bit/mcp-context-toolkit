"""Lexical capability search over a pre-built knowledge index.

WHY THIS EXISTS
---------------
An agent about to write a new function needs one question answered *before* the
first line: **does this already exist?** Name matching cannot answer it — the
function that normalises text for speech is called ``normalize_for_speech`` and
shares no token with "clean up a transcript".

This module answers it lexically: BM25 over descriptions (docstrings, module
summaries, plan goals), then **neighbourhood expansion** along the graph edges
that the index carries. The neighbour of a lexical hit is often the semantic
answer, which is how a keyword index recovers part of what embeddings would give
— without a model, a vector store, or a second thing that ages.

DESIGN NOTES
------------
*The tokenizer is injected, never reimplemented.* Index and query MUST split
words identically; two implementations would drift, and the failure mode is
silent (the index holds a term the query can never produce). The caller passes
the same callable that built the index.

*Nothing is decided here.* The result is a ranked list of candidates with a
stated reason for each. Whether a candidate fits is a judgement about meaning,
and that judgement belongs to the model reading them — a search that pretends to
decide is worse than one that admits it suggests.

*Staleness is reported, not hidden.* The index carries the commit it was built
from. Every answer says how far the tree has moved since, because "how far did
it drift" is the only honest form of the question.
"""
from __future__ import annotations

import json
import math
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

# How strongly a document's structural weight (how often its module is imported)
# lifts its score. Deliberately small: importance is a tiebreaker between
# comparable matches, never a reason to surface an unrelated one.
GEWICHT_ANTEIL = 0.35

# A neighbour inherits this share of its seed's score. Below the seed by
# construction — a neighbour is context, not a competing answer.
NACHBAR_DAEMPFUNG = 0.4

# How many seeds get expanded, and how many neighbours each contributes.
SEEDS_EXPANDIERT = 8
NACHBARN_JE_SEED = 3

STANDARD_LIMIT = 5

AUFTRAG = ("Passt einer? Dann nimm ihn. Wenn keiner passt, sag warum.")
LEER = ("nichts gefunden — Neubau als eigenes Modul pruefen (Q16). "
        "Das heisst NICHT 'nichts da': es heisst, dass diese Worte nichts "
        "getroffen haben. Anders fragen ist billiger als doppelt bauen.")


class Wissensindex:
    """A loaded knowledge index: documents, edges, and a BM25 posting list."""

    def __init__(self, daten: dict, tokenizer: Callable[[str], list]):
        self.daten = daten
        self.tokenize = tokenizer
        self.dokumente: list = daten.get("dokumente", [])
        self.bm25: dict = daten.get("bm25", {})
        self.stand: str = daten.get("stand", "unbekannt")
        # Adjacency, built once per load. Both directions: a caller asking about
        # a function wants the module that contains it just as much as the
        # functions it calls.
        self.nachbarn: dict[int, list] = {}
        id_zu_index = {d["id"]: i for i, d in enumerate(self.dokumente)}
        self._id_zu_index = id_zu_index
        for von, nach, typ in daten.get("kanten", ()):
            a, b = id_zu_index.get(von), id_zu_index.get(nach)
            if a is None or b is None or a == b:
                continue
            self.nachbarn.setdefault(a, []).append((b, typ, "→"))
            self.nachbarn.setdefault(b, []).append((a, typ, "←"))

    # -- scoring ----------------------------------------------------------
    def _bm25(self, terme: list) -> dict[int, float]:
        postings = self.bm25.get("postings", {})
        laengen = self.bm25.get("laengen", [])
        N = self.bm25.get("N", 0) or 1
        avgdl = self.bm25.get("avgdl", 1.0) or 1.0
        k1 = self.bm25.get("k1", 1.5)
        b = self.bm25.get("b", 0.75)

        punkte: dict[int, float] = {}
        for term in set(terme):
            liste = postings.get(term)
            if not liste:
                continue
            df = len(liste)
            idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
            for dok, tf in liste:
                dl = laengen[dok] if dok < len(laengen) else avgdl
                teil = idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
                punkte[dok] = punkte.get(dok, 0.0) + teil
        return punkte

    def suche(self, frage: str, limit: int = STANDARD_LIMIT,
              art: str | None = None) -> dict:
        """Ranked candidates for a natural-language question.

        Returns ``{treffer, weggelassen, begriffe}``. Each hit carries the
        reason it surfaced — a term match, or proximity to one.
        """
        terme = self.tokenize(frage)
        roh = self._bm25(terme)
        if not roh:
            return {"treffer": [], "weggelassen": 0, "begriffe": sorted(set(terme))}

        # Structural weight as a mild multiplier.
        for dok in list(roh):
            g = self.dokumente[dok].get("gewicht", 0.0)
            roh[dok] *= 1.0 + GEWICHT_ANTEIL * g

        rang = sorted(roh.items(), key=lambda kv: -kv[1])
        grund = {dok: "wort" for dok, _ in rang}

        # Neighbourhood expansion: what sits one edge from a strong hit.
        for dok, punkte in rang[:SEEDS_EXPANDIERT]:
            gezaehlt = 0
            for nachbar, typ, richtung in self.nachbarn.get(dok, ()):
                if gezaehlt >= NACHBARN_JE_SEED:
                    break
                geerbt = punkte * NACHBAR_DAEMPFUNG
                if geerbt <= roh.get(nachbar, 0.0):
                    continue
                roh[nachbar] = geerbt
                pfeil = "ruft" if richtung == "→" else "gerufen von"
                grund[nachbar] = (f"neben {self.dokumente[dok]['titel']} "
                                  f"({typ}, {pfeil})")
                gezaehlt += 1

        sortiert = sorted(roh.items(), key=lambda kv: -kv[1])
        if art:
            sortiert = [(d, p) for d, p in sortiert
                        if self.dokumente[d].get("art") == art]

        treffer = []
        for dok, punkte in sortiert[:limit]:
            d = self.dokumente[dok]
            treffer.append({
                "art": d["art"],
                "titel": d["titel"],
                "ziel": d["id"].split(":", 1)[1] if ":" in d["id"] else d["id"],
                "pfad": d.get("pfad", ""),
                "zeile": d.get("zeile", 0),
                "beschreibung": d.get("beschreibung", ""),
                "quelle": d.get("quelle", ""),
                "punkte": round(punkte, 3),
                "grund": grund.get(dok, "wort"),
            })
        return {
            "treffer": treffer,
            "weggelassen": max(0, len(sortiert) - limit),
            "begriffe": sorted(set(terme)),
        }


# ---------------------------------------------------------------------------
# Loading and staleness
# ---------------------------------------------------------------------------
def laden(pfad: Path, tokenizer: Callable[[str], list]) -> Wissensindex | None:
    try:
        return Wissensindex(json.loads(pfad.read_text(encoding="utf-8")), tokenizer)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def drift(stand: str, projekt: Path) -> dict:
    """How far has the tree moved since the index was built?

    ``{stand, head, dateien}`` — ``dateien`` is the number of files that differ
    between the index's commit and the working tree, or ``None`` when git cannot
    answer (no repo, unknown commit). ``None`` is reported as such: an unmeasured
    drift must not read like a measured zero.
    """
    out: dict[str, Any] = {"stand": stand, "head": None, "dateien": None}
    if stand in ("", "unbekannt"):
        return out
    try:
        out["head"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=projekt,
            capture_output=True, text=True, timeout=10).stdout.strip() or None
        roh = subprocess.run(
            ["git", "diff", "--name-only", stand], cwd=projekt,
            capture_output=True, text=True, timeout=20)
        if roh.returncode == 0:
            out["dateien"] = len([z for z in roh.stdout.splitlines() if z.strip()])
    except (OSError, subprocess.SubprocessError):
        pass
    return out


# ---------------------------------------------------------------------------
# Presentation — the shape IS the mechanism, not decoration
# ---------------------------------------------------------------------------
def formatiere(ergebnis: dict, drift_info: dict) -> str:
    """Render candidates as a decision, not as a wall of text.

    Three properties, each deliberate:

    1. **The instruction sits in the head of the answer.** A list without one
       invites skimming; a list with one forces either "I took it" or "here is
       why not", and both are useful.
    2. **One sentence per candidate.** A bare path forces opening the file —
       one more place to give up, in a chain where giving up is the failure mode.
    3. **Truncation is named.** "17 more, ask narrower" is information;
       silently showing five of twenty-two is a lie of omission.
    """
    zeilen = []
    kopf = f"Index-Stand {drift_info['stand']}"
    if drift_info.get("head") and drift_info["head"] != drift_info["stand"]:
        kopf += f", HEAD {drift_info['head']}"
    # Die Distanz haengt an `dateien`, NICHT an `head != stand`. `drift()` misst
    # gegen den ARBEITSBAUM, nicht gegen HEAD — der Normalfall einer Sitzung ist
    # `head == stand` mit schmutzigem Baum, weil der Index vom Post-Commit-Hook
    # kommt und man seitdem weitergebaut hat. Genau dann wird gefragt, und genau
    # dann verschwieg die erste Fassung die 12 geaenderten Dateien (Reviewer-Fund
    # 2026-08-04). Ein „nichts gefunden" aus einem Stand, der die vor zehn
    # Minuten geschriebene Funktion nicht kennt, ist die teuerste Auskunft, die
    # dieses Werkzeug geben kann.
    n = drift_info.get("dateien")
    if n:
        kopf += f", {n} Dateien seitdem geaendert"
    elif n is None:
        kopf += ", Abstand nicht messbar"
    zeilen.append(kopf)
    zeilen.append("")

    treffer = ergebnis["treffer"]
    if not treffer:
        zeilen.append(LEER)
        if ergebnis.get("begriffe"):
            zeilen.append(f"Gesucht wurde nach: {', '.join(ergebnis['begriffe'])}")
        return "\n".join(zeilen)

    kopfzeile = f"{len(treffer)} Kandidat{'en' if len(treffer) != 1 else ''}."
    if ergebnis["weggelassen"]:
        kopfzeile += (f" {ergebnis['weggelassen']} weitere weggelassen — "
                      f"enger fragen oder limit erhoehen.")
    zeilen.append(f"{kopfzeile} {AUFTRAG}")
    zeilen.append("")

    for i, t in enumerate(treffer, 1):
        ort = f"{t['pfad']}:{t['zeile']}" if t["zeile"] else t["pfad"]
        zeilen.append(f"{i}. [{t['art']}] {t['ziel']}")
        if ort and ort not in t["ziel"]:
            zeilen.append(f"   {ort}")
        zeilen.append(f"   -> {t['beschreibung']}")
        zeilen.append(f"   ({t['grund']}, Quelle der Beschreibung: "
                      f"{t['quelle'] or 'keine'})")
        zeilen.append("")
    return "\n".join(zeilen).rstrip()


# ---------------------------------------------------------------------------
# Source: locate, load, reload, and build-on-demand
# ---------------------------------------------------------------------------
class CapabilityQuelle:
    """Owns the index file: finds it, reloads it on change, builds it if absent.

    Reload is mtime-driven per call, like the rules and memory tiers — no
    watcher daemon, no stale in-memory copy after a rebuild.

    Building on demand exists for exactly one case: a fresh clone where the
    post-commit hook has not run yet. It is a subprocess, it is slow (seconds),
    and it happens once. The alternative — answering "nothing found" from an
    absent index — would be the worst possible lie this tool can tell, because
    "nothing found" is precisely the signal that licenses building something new.
    """

    def __init__(self, projekt: Path, index_pfad: Path,
                 tokenizer: Callable[[str], list], bauer: Path | None = None):
        self.projekt = projekt
        self.pfad = index_pfad
        self.tokenizer = tokenizer
        self.bauer = bauer
        self._index: Wissensindex | None = None
        self._mtime: float = -1.0
        # Serialises both the build and the load. Two concurrent tool calls on a
        # missing index would otherwise start two builds writing the same file.
        self._sperre = threading.Lock()
        # Negative cache: a build that failed once fails again for the same
        # reason. Retrying it on every call turns one broken environment into a
        # subprocess storm — and each attempt costs seconds of the answer the
        # caller is waiting for.
        self._bau_gescheitert = False

    def current(self) -> Wissensindex | None:
        with self._sperre:
            if not self.pfad.is_file() and (self._bau_gescheitert or not self._bauen()):
                self._bau_gescheitert = True
                return None
            try:
                mtime = self.pfad.stat().st_mtime
            except OSError:
                return None
            if self._index is None or mtime != self._mtime:
                self._index = laden(self.pfad, self.tokenizer)
                self._mtime = mtime
            return self._index

    def _bauen(self) -> bool:
        if self.bauer is None or not self.bauer.is_file():
            return False
        try:
            fertig = subprocess.run(
                ["python3", str(self.bauer), "--projekt", str(self.projekt),
                 "--out", str(self.pfad)],
                cwd=self.projekt, capture_output=True, text=True, timeout=300)
            return fertig.returncode == 0 and self.pfad.is_file()
        except (OSError, subprocess.SubprocessError):
            return False
