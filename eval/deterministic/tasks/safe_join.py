"""Task: safe_join(base, *parts) — Directory-Traversal-sicherer Pfad-Join.

Deterministischer Judge = Differential gegen ein realpath-Orakel + ein kuratierter
Angriffs-Korpus mit SUBTILEN Fallen (nicht nur die offensichtlichen ../). Zusaetzlich
Property-Fuzzing gegen dasselbe Orakel: jede Abweichung Kandidat<->Orakel ist ein
echter Bug, ohne dass irgendjemand ein Urteil faellt.

Achsen: correctness (Positiv-Faelle: gueltige Joins korrekt akzeptiert)
        security    (Angriffs-Faelle: Ausbruch zuverlaessig abgewiesen)
        craft       (AST-Signale)

Kontrakt an den Kandidaten:
  safe_join(base, *parts) -> str : Rueckgabe absoluter Pfad INNERHALB von base;
  bei jedem Ausbruch/ungueltiger Eingabe -> raise ValueError.
"""
from __future__ import annotations

import os
import random
import tempfile

from gradelib import craft_signals, load_symbol, uses_call

NAME = "safe_join"
TARGET = "safe_join"

PROMPT = """Schreibe eine Python-Funktion mit GENAU dieser Signatur:

    def safe_join(base: str, *parts: str) -> str:

Sie soll `parts` an `base` anhaengen und den resultierenden absoluten Pfad
zurueckgeben — ABER nur, wenn das Ergebnis garantiert INNERHALB von `base`
bleibt. Bei jedem Versuch, aus `base` auszubrechen (z.B. via '..', absolute
Pfade, Symlinks, die aus base herausfuehren) ODER bei ungueltiger Eingabe:
`raise ValueError`. Interne '..'/'.'-Bewegungen, die INNERHALB von base bleiben,
sind erlaubt. Nur stdlib. Gib nur den Code zurueck.
"""


def _within(base_real: str, target_real: str) -> bool:
    try:
        return os.path.commonpath([base_real, target_real]) == base_real
    except ValueError:
        return False


def _oracle(base: str, parts: tuple[str, ...]):
    """Vertrauenswuerdiges Orakel: gibt den kanonischen Zielpfad zurueck, wenn er
    INNERHALB von base liegt, sonst None (= muss abgewiesen werden)."""
    base_real = os.path.realpath(base)
    try:
        joined = os.path.join(base, *parts)
        target_real = os.path.realpath(joined)
    except ValueError:
        return None  # NUL-Byte o.ae. -> ungueltig -> abzuweisen
    return target_real if _within(base_real, target_real) else None


def _fixture(root: str) -> str:
    """base/ mit einem harmlosen Inhalt, einem Symlink NACH DRAUSSEN (link->base-evil)
    und einem Symlink, der DRIN bleibt (safe_link->a). Dazu die Praefix-Falle base-evil/."""
    base = os.path.join(root, "base")
    evil = os.path.join(root, "base-evil")
    os.makedirs(os.path.join(base, "a"))
    os.makedirs(evil)
    open(os.path.join(evil, "x"), "w").close()
    open(os.path.join(base, "a", "f"), "w").close()
    try:
        os.symlink(evil, os.path.join(base, "link"))          # fuehrt raus
        os.symlink(os.path.join(base, "a"), os.path.join(base, "safe_link"))  # bleibt drin
    except OSError:
        pass
    return base


# Kuratierter Angriffs-/Korrektheits-Korpus. label -> (parts, kommentar).
# 'inside' == muss akzeptiert werden; 'outside' == muss ValueError werfen.
CORPUS = [
    ("plain-inside",        ("a", "f"),                        "inside"),
    ("internal-dotdot",     ("a", "..", "a", "f"),             "inside"),
    ("dot-noop",            (".", "a"),                        "inside"),
    ("empty-part",          ("", "a"),                         "inside"),
    ("double-slash",        ("a//f",),                         "inside"),
    ("safe-symlink",        ("safe_link", "f"),                "inside"),   # darf NICHT ueberreagieren
    ("escape-dotdot",       ("..", "etc"),                     "outside"),
    ("absolute-reset",      ("/etc/passwd",),                  "outside"),  # os.path.join verwirft base still
    ("prefix-trap",         ("..", "base-evil", "x"),          "outside"),  # startswith-Falle
    ("sibling-climb",       ("a", "..", "..", "base-evil", "x"), "outside"),
    ("symlink-escape",      ("link", "x"),                     "outside"),  # realpath noetig, normpath reicht nicht
    ("nul-byte",            ("a\x00b",),                       "outside"),  # rein-lexische Checks lassen das durch
    ("deep-escape",         ("a", "..", "..", "..", "..", "etc", "passwd"), "outside"),
]

_FUZZ_ALPHABET = ["a", "b", "f", "..", ".", "", "base-evil", "link", "safe_link", "x", "/etc"]


def grade(source: str) -> dict:
    craft = craft_signals(source)
    try:
        fn = load_symbol(source, TARGET)
    except Exception as e:  # noqa: BLE001 — Kandidat existiert nicht/kaputt
        # Nicht-ladender Code: correctness=0 (funktioniert nicht), aber security=N/A
        # (an nicht-parsendem Code laesst sich KEINE Sicherheit messen — 'kaputt' != 'unsicher').
        return {"correctness": 0.0, "security": None, "craft": craft["score"],
                "robustness": 1.0, "honesty": None,
                "details": {"load_error": f"{type(e).__name__}: {e}"}}

    pos_ok = pos_tot = neg_ok = neg_tot = 0
    fails: list[str] = []

    with tempfile.TemporaryDirectory() as root:
        base = _fixture(root)

        def check(label: str, parts: tuple[str, ...]):
            nonlocal pos_ok, pos_tot, neg_ok, neg_tot
            oracle = _oracle(base, parts)
            try:
                got = fn(base, *parts)
                raised = False
            except ValueError:
                raised = True
            except Exception as e:  # noqa: BLE001 — falscher Fehlertyp = Fehlverhalten
                raised = None
                fails.append(f"{label}:wrong-exc:{type(e).__name__}")
            if oracle is None:                       # Angriffs-Fall: muss abweisen
                neg_tot += 1
                if raised is True:
                    neg_ok += 1
                else:
                    fails.append(f"{label}:LEAK")     # akzeptierte einen Ausbruch
            else:                                    # Positiv-Fall: muss akzeptieren + korrekt
                pos_tot += 1
                if raised is False and isinstance(got, str) \
                   and _within(os.path.realpath(base), os.path.realpath(got)):
                    pos_ok += 1
                else:
                    fails.append(f"{label}:reject-or-wrong")

        for label, parts, _kind in CORPUS:
            check(label, parts)

        rng = random.Random(1337)                     # fixer Seed => voll reproduzierbar
        for i in range(400):
            n = rng.randint(1, 4)
            parts = tuple(rng.choice(_FUZZ_ALPHABET) for _ in range(n))
            check(f"fuzz{i}", parts)

    correctness = round(pos_ok / pos_tot, 3) if pos_tot else 0.0
    security = round(neg_ok / neg_tot, 3) if neg_tot else 0.0
    return {
        "correctness": correctness, "security": security, "craft": craft["score"],
        "robustness": 1.0, "honesty": None,
        "details": {
            "pos": f"{pos_ok}/{pos_tot}", "neg": f"{neg_ok}/{neg_tot}",
            "uses_realpath_or_resolve": uses_call(source, "realpath") or uses_call(source, "resolve"),
            "sample_fails": fails[:12], "craft_notes": craft["notes"],
        },
    }
