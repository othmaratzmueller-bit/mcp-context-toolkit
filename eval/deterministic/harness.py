#!/usr/bin/env python3
"""Deterministischer Eval-Harness — der Judge ist Code, kein LLM.

Gegenentwurf zum LLM-Judge-Eval 2026-07-12: ein Modell schreibt Code gegen einen
fixen Kontrakt; eine LOKALE, deterministische Test-Batterie bewertet ihn. Kein
Opus/Sonnet-Panel, keine Meinung, keine >12-EUR-Judge-Runde — dieselbe Antwort
ergibt fuer immer denselben Score, und neu ergaenzte Fallen bewerten ALTE
Antworten gratis neu.

Pro Aufgabe mehrere ORTHOGONALE Achsen, nie ein Sammel-Score (Owner 2026-07-16:
"das sehe ich in keinem benchmark"). Jede Achse hat eine deterministische Technik:
  correctness : Funktions-Batterie + Property-Fuzzing gegen ein Orakel
  security    : Angriffs-Korpus (hand-gelabelt) + Differential gegen ein stdlib-
                Orakel (ipaddress, os.path.realpath)
  honesty     : Patch-Aufgabe -> AST-Diff (Funktion verbogen oder Test korrigiert?)
  robustness  : Kandidat laeuft im Subprozess mit Hard-Timeout; Loop/Hang = 0.
                (Generierungs-Loops zusaetzlich aus den Metadaten, siehe grade_output.py)
  craft       : reine AST-Signale (bare-except, magic numbers, dead code, cc) —
                bewusst als GROBER Proxy deklariert

Jeder Kandidat wird in einem frischen Subprozess (--worker) mit Hard-Timeout
gegradet: isoliert Crashes, faengt Endlos-Loops deterministisch als robustness=0.
stdlib-only. Keine Netz-Calls im Grader.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from gradelib import AXES, strip_code  # noqa: E402  (nach sys.path-Setup)


def grade_source(source: str, task_name: str, timeout: float = 25.0) -> dict:
    """Gradet EINEN Kandidaten deterministisch in einem frischen Subprozess.
    Timeout/Crash -> robustness=0 (Loop deterministisch erkannt). Rueckgabe:
    {axis: float|None, "details": {...}}."""
    try:
        proc = subprocess.run(
            [sys.executable, str(HERE / "harness.py"), "--worker", "--task", task_name],
            input=source, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {**{a: 0.0 for a in AXES},
                "details": {"error": "timeout", "hint": "Kandidat haengt/loopt (Hard-Timeout)"}}
    if proc.returncode != 0 or not proc.stdout.strip():
        return {**{a: 0.0 for a in AXES},
                "details": {"error": "crash", "stderr": proc.stderr[-800:]}}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {**{a: 0.0 for a in AXES},
                "details": {"error": "bad-worker-output", "stdout": proc.stdout[-800:]}}


def _worker(task_name: str) -> int:
    """Laeuft IM Subprozess: liest Kandidaten-Source von stdin, gradet, druckt JSON."""
    from tasks import TASKS
    source = strip_code(sys.stdin.read())
    task = TASKS[task_name]
    try:
        result = task.grade(source)
    except Exception as e:  # noqa: BLE001 — Worker faultet nie hart; Score=0 IST das Urteil
        result = {**{a: 0.0 for a in AXES}, "details": {"error": f"{type(e).__name__}: {e}"}}
    for a in AXES:
        result.setdefault(a, None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worker", action="store_true", help="intern: gradet stdin-Source")
    ap.add_argument("--task", default="")
    a = ap.parse_args()
    if a.worker:
        return _worker(a.task)
    ap.error("harness.py wird von selftest.py / grade_output.py benutzt, nicht direkt")
    return 2


if __name__ == "__main__":
    sys.exit(main())
