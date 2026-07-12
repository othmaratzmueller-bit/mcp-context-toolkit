# Code-Quality-Eval — „ein echtes Ziel"

Ergänzt den Reward-Hack-Eval (`../run_eval.py`, misst **Boden-Disziplin**) um die
**Decke**: wie nah kommt ein Modell an eine **Gold-Referenz** heran, bei einer echten,
vollständig definierten Coding-Aufgabe? Nicht „hackt es", sondern „ist der Code korrekt,
sicher und sauber".

**Zweck (Owner 2026-07-12):** dauerhaft abgelegt, damit man ihn gegen **jedes neue Modell**
und **jede verschärfte Regel** wiederfahren kann — ein stehendes Ziel statt Einmal-Messung.

## Bestandteile

| Datei | Rolle |
|---|---|
| `task.md` / `task_prompt.txt` | die EINE Aufgabe (`safe_join`, security-first) — human-doc + exakter Prompt |
| `references/opus.py`, `references/sonnet.py` | Gold-Referenzen (Opus + Sonnet, delegiert erzeugt) = das Ziel |
| `references/test_references.py` | Korrektheits-Orakel: beweist, dass beide Referenzen die 4 Killer-Edge-Cases dicht machen (Traversal, absolute, Symlink-Grenze, `/base`-vs-`/base-evil`) |
| `run_codequality.py` | Subjekt-Runner: ein Modell × 3 Arme × Thinking → volle Antwort + Reasoning + Token + **Kosten (€)** + **Latenz (`latency_s`, `tokens_per_s`)** |
| `sweep.sh` | Driver: alle Modelle × Thinking (an/aus wo möglich) |

## Achsen (Judge gegen die Referenzen)

- **security 0-3** — blockt es Traversal wirklich? (die 4 Edge-Cases)
- **quality 0-3** — Korrektheit + Idiomatik + Vollständigkeit + Fehler-Klarheit
- **correct bool** — Kern-Kontrakt erfüllt

Ein Modell kann „läuft" liefern und trotzdem die Sicherheitslücke lassen — security ≠ quality.

## Ausführen

```bash
# 1. Referenzen selbst-testen (muss GRÜN sein, sonst ist der Maßstab kaputt):
python3 references/test_references.py

# 2. Subjekt-Läufe (ein Modell):
export OPENROUTER_API_KEY=sk-or-...
python3 run_codequality.py --label deepseek-v4-flash --model deepseek/deepseek-v4-flash \
    --base-url https://openrouter.ai/api/v1 --mode or --thinking on --out out/deepseek-v4-flash_on.json

# oder alle auf einmal:
bash sweep.sh
```

## Judging (gegen die Gold-Referenzen)

Jede Subjekt-Lösung wird gegen `references/{opus,sonnet}.py` bewertet — durch ein **starkes
Panel**. Zwei Wege:
- **Claude-Code (bevorzugt, Anthropic-Judges „hier"):** ein Workflow mit Opus+Sonnet-Agenten
  liest die Subjekt-Antwort + die Referenzen + `task.md` und vergibt security/quality/correct.
  Zwei unabhängige Richter → Kreuzvalidierung.
- **Portabel:** ein starkes OpenRouter-Modell als Judge (analog `../run_eval.py`), falls
  ausserhalb dieser Umgebung reproduziert wird.

## Neues Modell / verschärfte Regel testen

- **Neues Modell:** Preis in `run_codequality.py::PRICING` + eine Zeile in `sweep.sh` → laufen lassen.
- **Regel verschärft:** `../arms/grundregeln_v2.md` bzw. `../arms/method.md` anpassen → re-run;
  der Vergleich baseline vs Regeln vs Regeln+Workflow zeigt, ob die Verschärfung wirkt.
- **Aufgabe erweitern:** weitere Bausteine als `task2.md` + Referenzen — der Runner ist auf
  eine Aufgabe pro Lauf ausgelegt; für mehrere Bausteine mehrere Läufe.

## Grenzen (ehrlich)

EINE Aufgabe = richtungsweisend, kein breiter Skill-Bench. Für harte Aussagen mehrere
Bausteine + reps. Der Judge ist stark, aber Code-Review bleibt teils Geschmack (Opus/Sonnet
waren sich bei Reward-Hack-Qualität zu 98% |Δ|≤1 einig — gut, nicht perfekt).
