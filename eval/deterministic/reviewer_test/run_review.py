#!/usr/bin/env python3
"""Schickt den Review-Prompt (traps.REVIEW_PROMPT) an ein Reviewer-Modell und speichert
die rohe Prosa-Antwort nach out/<label>.txt. Kein Judge hier — gejudged wird lokal +
deterministisch (score.py, Anker-Match). Key via --api-key-file, nie geloggt."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from traps import REVIEW_PROMPT  # noqa: E402


def chat(base, key, model, prompt, extra, timeout=400, retries=3):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    payload.update(extra)
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                         data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last = e; time.sleep(2 * (a + 1))
    raise RuntimeError(str(last))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    ap.add_argument("--api-key-file", required=True)
    ap.add_argument("--thinking", choices=["on", "off"], default="on")
    ap.add_argument("--max-tokens", type=int, default=30000)
    a = ap.parse_args()

    key = Path(a.api_key_file).read_text(encoding="utf-8").strip()
    extra = {"max_tokens": a.max_tokens,
             "reasoning": {"effort": "high"} if a.thinking == "on" else {"enabled": False}}
    t0 = time.perf_counter()
    try:
        d = chat(a.base_url, key, a.model, REVIEW_PROMPT, extra)
    except RuntimeError as e:
        print(f"[{a.label}] kaputt: {e}", file=sys.stderr)
        (HERE / "out" / f"{a.label}.txt").write_text("", encoding="utf-8")
        return 1
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    content = msg.get("content") or ""
    u = d.get("usage") or {}
    meta = {"label": a.label, "model": a.model, "finish": ch.get("finish_reason"),
            "content_len": len(content), "completion_tokens": u.get("completion_tokens"),
            "cost": (u.get("cost") if isinstance(u.get("cost"), (int, float)) else None),
            "latency_s": round(time.perf_counter() - t0, 1)}
    (HERE / "out" / f"{a.label}.txt").write_text(content, encoding="utf-8")
    (HERE / "out" / f"{a.label}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"[{a.label}] {meta['content_len']} Zeichen, finish={meta['finish']}, {meta['latency_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
