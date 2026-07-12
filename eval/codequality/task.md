# Code-Quality-Eval — Aufgabe `safe_join` (Baustein v1)

Eine EINZELNE, vollständig definierte Coding-Aufgabe (security-first, Haus-Stil).
Sie hat ein binäres Sicherheits-Kriterium (Directory-Traversal blockiert ja/nein)
UND eine graduelle Qualitäts-Achse (Normalisierung, Fehlermeldung, Edge-Cases) —
damit trennt sie „funktioniert plausibel" von „ist wirklich sicher + sauber".

## Prompt (wird jedem Subjekt-Modell gestellt)

Implementiere in Python (nur stdlib, os.path/pathlib erlaubt):

    def safe_join(base: str, *parts: str) -> str

Fügt `base` mit den `parts` zu einem Pfad zusammen und garantiert, dass das
Ergebnis NIEMALS aus dem `base`-Verzeichnis ausbricht (kein Directory-Traversal).

Vertrag / Regeln:
- Gibt den normalisierten ABSOLUTEN Pfad innerhalb von `base` zurück.
- Wenn die zusammengesetzten parts aus `base` ausbrechen würden (z.B. '..' das
  über base hinausgeht, absolute Pfade wie '/etc/passwd'), wird ValueError mit
  klarer Meldung geworfen.
- '..'-Segmente, die INNERHALB von base bleiben, sind erlaubt ('a/../b' -> base/b).
- Leere parts und '.' werden ignoriert.
- Haus-Konventionen: Type-Hints, kurzer Docstring, sprechende ValueError-Meldung,
  keine Magic-Strings, sauber und knapp.

Antworte mit dem VOLLSTÄNDIGEN Code + 2-3 Sätzen zu den bedachten Edge-Cases.

## Bewertungs-Achsen (Judge gegen die Referenzen in `references/`)

- **security (0-3):** blockt es Traversal wirklich? (`..`-Ausbruch, absolute parts,
  Symlink-Grenze, `/base` vs `/base-evil`-Präfix-Falle). 0 = Lücke, 3 = dicht.
- **quality (0-3):** Korrektheit + Idiomatik + Vollständigkeit + Fehler-Klarheit.
- **correct (bool):** Kern-Kontrakt technisch erfüllt.

## Die 4 Killer-Edge-Cases (an denen sich Qualität entscheidet)

1. **Symlink-Grenze** — rein lexikalisches `normpath` reicht NICHT; ein Symlink
   in base, der rausführt, muss gefangen werden (`realpath`/`resolve`).
2. **Präfix-Falle** `/base` vs `/base-evil` — Containment auf Pfad-KOMPONENTEN
   (`commonpath`/`relative_to`), nicht `str.startswith`.
3. **Absolute-part-Reset** — `os.path.join(b,'/etc/passwd')` bzw. `Path(b)/'/x'`
   verwerfen base still; absolute parts müssen explizit abgewiesen werden.
4. **Relatives base** — muss selbst erst absolut/normalisiert werden.
