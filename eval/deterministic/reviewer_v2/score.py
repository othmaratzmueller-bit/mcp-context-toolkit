#!/usr/bin/env python3
"""Gradet die v2-Reviews (out/*.txt): fand das Modell den Verknüpfungs-Bug, und ist
es auf die Decoys reingefallen? Kein LLM-Judge — Nähe-Anker + Handlese-Verifikation."""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from traps import grade  # noqa: E402


def main() -> int:
    files = sorted(glob.glob(str(HERE / "out" / "*.txt")))
    if not files:
        print("keine Reviews in out/ — erst run_review.py laufen lassen"); return 1

    rows = []
    for f in files:
        label = Path(f).stem
        text = Path(f).read_text(encoding="utf-8")
        meta_p = HERE / "out" / f"{label}.meta.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        g = grade(text)
        rows.append((label, g, len(text), meta.get("latency_s"), meta.get("cost")))

    # Sortierung: Bug gefunden zuerst, dann weniger Decoy-Fehlalarme
    rows.sort(key=lambda r: (not r[1]["found_linkage"],
                             r[1]["flagged_receipt_decoy"] + r[1]["flagged_percent_decoy"]))

    print("=" * 84)
    print("REVIEWER v2 — Verknüpfungs-Bug (billing charge /100) gefunden? Decoys standgehalten?")
    print("=" * 84)
    h = f"{'Modell':17s} | {'Bug gefunden':13s} | {'receipt-Decoy':14s} {'percent-Decoy':14s} | {'Zeit':>6} {'$':>7}"
    print(h); print("-" * len(h))
    for label, g, ln, lat, cost in rows:
        bug = "  ✓ JA" if g["found_linkage"] else "  ✗ nein"
        rd = "Fehlalarm" if g["flagged_receipt_decoy"] else "ok"
        pd = "Fehlalarm" if g["flagged_percent_decoy"] else "ok"
        print(f"{label:17s} | {bug:13s} | {rd:14s} {pd:14s} | {str(lat)+'s':>6} {cost if cost else '?':>7}")
    print("-" * len(h))
    print("(jedes ✓/Fehlalarm wird von Hand gegen die Roh-Review verifiziert)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
