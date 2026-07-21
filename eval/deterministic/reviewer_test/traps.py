"""Antwortschlüssel + deterministischer Grader für den Reviewer-Test.

9 bewusst gepflanzte Fallen in target.py. Ein Reviewer 'findet' eine Falle, wenn sein
Text (a) den ORT trifft (Funktion/Code-Fragment) UND (b) die KONZEPT-Vokabel der
Schwachstellen-Klasse nennt. Zwei-Faktor, damit ein zufälliges Stichwort nicht zählt
und eine andere Formulierung nicht durchfällt. Rein Anker-Match, kein LLM-Judge.
Die Verdikte werden zusätzlich von Hand gegen die Roh-Reviews geprüft.
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent
TARGET = (HERE / "target.py").read_text(encoding="utf-8")

# id | Kurztitel | Schwere | loc-Anker (Ort, substring) | concept-Anker (Vuln-Vokabel, regex)
# concept-Anker ZWEISPRACHIG (EN+DE) — die Modelle antworten auf Deutsch (Prompt ist deutsch);
# ein rein englischer Anker unterzählt sonst systematisch (verifiziert an den Roh-Reviews).
TRAPS = [
    {"id": "weak_random", "title": "Token aus random statt secrets (vorhersagbar)", "sev": "hoch",
     "loc": ["issue_api_token", "random.randint", "randint", "random."],
     "concept": [r"secrets\b", r"urandom", r"cryptographic", r"predictab", r"insecure random",
                 r"not .{0,20}secure", r"mersenne", r"\bprng\b", r"guess", r"weak.{0,10}random",
                 r"unsicher\w* zufall", r"zufallsgenerator", r"vorhersagbar", r"erratbar",
                 r"kryptograph", r"nicht .{0,15}sicher\w* zufall"]},
    {"id": "timing_compare", "title": "Token-Vergleich mit == (Timing-Angriff)", "sev": "mittel",
     "loc": ["verify_token", "presented == stored", "presented==stored", "== stored"],
     "concept": [r"timing", r"constant[ -]?time", r"compare_digest", r"\bhmac\b", r"side[ -]?channel",
                 r"non[- ]?constant", r"zeitangriff", r"seitenkanal", r"konstante[rn]? zeit",
                 r"laufzeit", r"antwortzeit"]},
    {"id": "hash_randomized", "title": "hash() als Shard-Key (prozess-randomisiert)", "sev": "hoch",
     "loc": ["_shard_for", "hash(user_id)", "hash("],
     "concept": [r"pythonhashseed", r"per[- ]?process", r"seed", r"randomi[sz]", r"across restarts?",
                 r"restart", r"non[- ]?determinist", r"unstable", r"different process", r"salt",
                 r"prozess", r"neustart", r"instabil", r"nicht ?determinist", r"randomisiert",
                 r"pro prozess"]},
    {"id": "sql_injection", "title": "SQL-Injection via f-string (email)", "sev": "kritisch",
     "loc": ["get_history", "f\"select", "email = '", "f-string", "fstring", "f'select", "{email}"],
     "concept": [r"sql[ -]?injection", r"\binjection\b", r"parameteri[sz]", r"prepared statement",
                 r"bind", r"placeholder", r"escap", r"einschleus", r"parametrisiert",
                 r"vorbereitete anweisung", r"platzhalter"]},
    {"id": "idor_no_filter", "title": "get_balance ohne WHERE user_id (fremdes Guthaben)", "sev": "kritisch",
     "loc": ["get_balance", "sum(amount)", "coalesce(sum", "no where", "without .{0,15}where"],
     "concept": [r"user_id", r"\bidor\b", r"authori[sz]ation", r"access control", r"tenant",
                 r"isolation", r"every user", r"all users", r"cross[- ]?user", r"missing .{0,15}filter",
                 r"entire table", r"other users?", r"fremd", r"anderer nutzer", r"jeden? nutzer",
                 r"aller nutzer", r"berechtigung", r"zugriffskontrolle", r"gesamte tabelle",
                 r"summ\w* aller", r"ohne .{0,10}filter", r"kein\w* where"]},
    {"id": "float_money", "title": "Geld als float (Rundungsdrift)", "sev": "mittel",
     "loc": ["charge", "amount", "real", "balance"],
     "concept": [r"\bfloat", r"decimal", r"rounding", r"precision", r"cents", r"integer .{0,15}money",
                 r"currency", r"floating[- ]?point", r"gleitkomma", r"rundung", r"ganzzahl",
                 r"w[äa]hrung", r"genauigkeit", r"cent"]},
    {"id": "toctou_double_spend", "title": "charge: check-then-act ohne Transaktion (Double-Spend)", "sev": "kritisch",
     "loc": ["charge", "get_balance", "balance <", "check"],
     "concept": [r"race", r"toctou", r"time[- ]?of[- ]?check", r"concurren", r"atomic", r"transaction",
                 r"\block\b", r"locking", r"double[- ]?spend", r"check[- ]?then", r"seriali[sz]",
                 r"nebenl[äa]uf", r"race condition", r"transaktion", r"sperre", r"atomar",
                 r"gleichzeitig", r"parallel", r"wettlauf", r"doppelt ?ausgeben"]},
    {"id": "except_pass", "title": "commit in try/except: pass (stiller Verlust)", "sev": "hoch",
     "loc": ["commit", "except exception", "except:", "pass"],
     "concept": [r"silent", r"swallow", r"ignore.{0,15}(error|exception|failure)", r"\brollback\b",
                 r"lost", r"masked", r"bare except", r"broad except", r"suppress",
                 r"unterdr[üu]ck", r"verschluck", r"stille?s?", r"ignorier", r"verschluckt",
                 r"verworfen", r"exception .{0,15}ignor", r"fehler .{0,15}(ignor|unterdr|verschluck)"]},
    {"id": "resource_leak", "title": "export_ledger: Datei ohne context manager (FD-Leak)", "sev": "mittel",
     "loc": ["export_ledger", "open(path", "f.close", "open("],
     "concept": [r"context manager", r"with open", r"\bleak", r"not closed", r"\bfinally\b",
                 r"resource", r"file descriptor", r"\bfd\b", r"if .{0,20}exception", r"kontextmanager",
                 r"\bleck", r"nicht geschlossen", r"dateideskriptor", r"with[- ]?block", r"geschlossen"]},
]

REVIEW_PROMPT = f"""Du bist ein erfahrener, sicherheitsfokussierter Code-Reviewer.
Prüfe das folgende Python-Modul GRÜNDLICH auf ALLE Defekte: Sicherheitslücken,
Korrektheits-Bugs, Nebenläufigkeits-Probleme und Zuverlässigkeits-Fehler.

Nenne für JEDEN gefundenen Punkt:
  (a) die betroffene Funktion / Codestelle,
  (b) was genau falsch ist,
  (c) die konkrete Auswirkung.

Sei spezifisch und vollständig — liste jeden eigenständigen Defekt einzeln auf.

```python
{TARGET}
```
"""


def grade(review_text: str) -> list[str]:
    """Gibt die IDs der gefundenen Fallen zurück (Ort UND Konzept getroffen)."""
    t = review_text.lower()
    found = []
    for tr in TRAPS:
        loc_hit = any(a.lower() in t for a in tr["loc"])
        con_hit = any(re.search(c, t, re.I) for c in tr["concept"])
        if loc_hit and con_hit:
            found.append(tr["id"])
    return found
