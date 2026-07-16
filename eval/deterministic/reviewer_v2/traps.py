"""Antwortschlüssel + Grader für den v2-Reviewer-Test (Verknüpfungsfehler).

EIN echter Bug, cross-file: billing.charge_subscription übergibt
`plan.price_cents / 100` an wallet.charge — aber charge ERWARTET Cent (money-
Invariante). Folge: 100-fache Unterberechnung + float bricht die int-Cent-Invariante.
Nur sichtbar, wenn man die Referenz billing -> wallet/money verfolgt.

ZWEI Decoys (sehen verdächtig aus, sind aber korrekt): receipt.py macht dasselbe
`/100` — aber für die ANZEIGE (korrekt); money.apply_percent hat ein float-
Zwischenprodukt, rundet aber sofort auf int-Cent (korrekt). Ein Pattern-Matcher
flaggt die Decoys; ein projekt-bewusster Reviewer nur den echten Bug.

Grader: (1) linkage-Bug gefunden? (Ort billing/`/100` UND Konzept Einheit/Cent/
Unterberechnung), (2) Decoys fälschlich als Bug geflaggt? Zweisprachig, plus Handlese.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent
PKG = HERE / "minibank"

# Reihenfolge: Kontext-Files zuerst, neues Feature zuletzt.
_ORDER = ["money.py", "plans.py", "wallet.py", "receipt.py", "billing.py"]
_BLOB = "\n\n".join(
    f"# ==================== minibank/{name} ====================\n"
    + (PKG / name).read_text(encoding="utf-8")
    for name in _ORDER
)

REVIEW_PROMPT = f"""Du bist ein erfahrener Code-Reviewer. Unten das kleine Projekt `minibank`
(Geld ist projektweit in ganzzahligen CENT — siehe money.py). Ein neues Feature
zur Abo-Abrechnung wurde gerade ergänzt: `billing.py` und `receipt.py`.

Reviewe das NEUE Feature IM KONTEXT des Projekts. Finde jeden Korrektheits-Defekt
— besonders Fehler, die erst durch das Zusammenspiel mit den bestehenden Modulen
(money/plans/wallet) sichtbar werden. Nenne pro Punkt: (a) Datei/Funktion,
(b) was falsch ist, (c) die konkrete Auswirkung. Sei präzise.

```python
{_BLOB}
```
"""

# --- Grader (Nähe-basiert, mit Entlastungs-Erkennung) ---------------------

# Bug-STELLE: die Abbuchung in billing (nicht die Anzeige in receipt).
LINK_SITE = [r"charge_subscription", r"wallet\.charge", r"charge\(plan", r"charge\(.{0,20}/ ?100"]
# FEHLER-Konzept: warum das /100 hier falsch ist.
LINK_WRONG = [
    r"erwartet .{0,12}cent", r"expects .{0,12}cents", r"already .{0,12}cents", r"schon .{0,12}cent",
    r"bereits .{0,12}cent", r"100[- ]?fach", r"\b100x\b", r"factor .{0,6}100", r"faktor .{0,6}100",
    r"undercharg", r"unterberechn", r"zu wenig", r"zu niedrig", r"1/100", r"hundred(th)?",
    r"should not .{0,10}divid", r"nicht .{0,10}(durch 100|teilen|dividieren)", r"ohne .{0,6}/ ?100",
    r"euro .{0,15}statt .{0,15}cent", r"cent .{0,15}statt .{0,15}euro", r"wrong unit",
    r"falsche .{0,8}einheit", r"unit mismatch", r"gleitkomma", r"\bfloat", r"invariant",
    r"9[.,]99", r"\b999\b", r"9 ?cent", r"9,?99 ?cent",
]
DECOY_RECEIPT = [r"receipt"]
DECOY_PERCENT = [r"apply_percent"]
BUGWORD = [r"\bbug", r"fehler", r"defect", r"defekt", r"inkonsist", r"inconsist",
           r"falsch", r"wrong", r"incorrect", r"violat", r"verletz", r"bricht", r"break"]
# Entlastung: der Decoy wird als KORREKT/harmlos benannt.
EXONERATE = [r"korrekt", r"richtig", r"\bfine\b", r"\bok\b", r"anzeige", r"display", r"nur .{0,10}anzeige",
             r"only .{0,10}display", r"kein.{0,6}bug", r"not a bug", r"absichtlich", r"intended",
             r"harmlos", r"in ordnung", r"unproblematisch", r"zulässig", r"erlaubt"]


def _near(t: str, anchors_a, anchors_b, window: int = 260) -> bool:
    for m in re.finditer("|".join(anchors_a), t, re.I):
        seg = t[max(0, m.start() - window): m.end() + window]
        if any(re.search(b, seg, re.I) for b in anchors_b):
            return True
    return False


def grade(text: str) -> dict:
    t = text.lower()
    found_linkage = _near(t, LINK_SITE, LINK_WRONG)

    def decoy_flagged(names) -> bool:
        for m in re.finditer("|".join(names), t, re.I):
            seg = t[max(0, m.start() - 220): m.end() + 220]
            bug = any(re.search(b, seg, re.I) for b in BUGWORD)
            exo = any(re.search(e, seg, re.I) for e in EXONERATE)
            if bug and not exo:
                return True
        return False

    return {
        "found_linkage": found_linkage,
        "flagged_receipt_decoy": decoy_flagged(DECOY_RECEIPT),
        "flagged_percent_decoy": decoy_flagged(DECOY_PERCENT),
    }
