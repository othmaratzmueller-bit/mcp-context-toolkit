#!/usr/bin/env python3
"""Gradet die Roh-Reviews in out/*.txt deterministisch gegen den Antwortschlüssel
(traps.TRAPS): welche der 9 gepflanzten Fallen hat jedes Modell gefunden? Rendert
eine Fallen×Modell-Matrix. Kein LLM-Judge — reines Anker-Match."""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from traps import TRAPS, grade  # noqa: E402


def main() -> int:
    files = sorted(glob.glob(str(HERE / "out" / "*.txt")))
    if not files:
        print("keine Reviews in out/ — erst run_review.py laufen lassen"); return 1

    results = {}
    for f in files:
        label = Path(f).stem
        text = Path(f).read_text(encoding="utf-8")
        meta_p = HERE / "out" / f"{label}.meta.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        results[label] = {"found": set(grade(text)), "len": len(text),
                          "finish": meta.get("finish"), "cost": meta.get("cost")}

    trap_ids = [t["id"] for t in TRAPS]
    labels = sorted(results, key=lambda m: -len(results[m]["found"]))

    print("=" * (26 + 3 * len(labels)))
    print(f"REVIEWER-TEST — {len(trap_ids)} versteckte Fallen, gefunden? (✓/·)  Judge = Anker-Match")
    print("=" * (26 + 3 * len(labels)))
    head = f"{'Falle':30s} {'Sev':9s} | " + " ".join(f"{m[:10]:>11}" for m in labels)
    print(head); print("-" * len(head))
    for t in TRAPS:
        row = f"{t['title'][:30]:30s} {t['sev']:9s} | "
        row += " ".join(f"{'  ✓ ' if t['id'] in results[m]['found'] else '  · ':>11}" for m in labels)
        print(row)
    print("-" * len(head))
    tot = f"{'GEFUNDEN / 9':30s} {'':9s} | "
    tot += " ".join(f"{str(len(results[m]['found']))+'/9':>11}" for m in labels)
    print(tot)
    fin = f"{'finish / len':30s} {'':9s} | "
    fin += " ".join(f"{(str(results[m]['finish'] or '?')[:4]+' '+str(results[m]['len'])):>11}" for m in labels)
    print(fin)

    print("\nWas jedes Modell VERPASST hat:")
    for m in labels:
        missed = [t["title"][:38] for t in TRAPS if t["id"] not in results[m]["found"]]
        print(f"  {m:16s} ({len(results[m]['found'])}/9): " + ("— alle gefunden" if not missed else "; ".join(missed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
