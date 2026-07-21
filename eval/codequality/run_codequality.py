#!/usr/bin/env python3
"""Code-Quality-Eval-Runner (stdlib-only, ein Modell pro Aufruf). Stellt jedem Modell
die EINE safe_join-Aufgabe (task.md) in 3 Armen (nackt / Regeln / Regeln+Workflow) x
Thinking an/aus, erfasst die GANZE Antwort + Reasoning + Token + Kosten. KEIN Judge hier
— gejudged wird gegen die Gold-Referenzen (references/) von einem starken Panel
(z.B. Claude-Code Opus+Sonnet oder ein starkes OpenRouter-Modell).

Reuse der Arme aus ../arms/ (grundregeln_v2, method). Thinking-Handling wie im
Reward-Hack-Runner: OpenRouter reasoning:{effort|enabled:false}, llama enable_thinking.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ARMS_DIR = HERE.parent / "arms"
TASK_PROMPT = (HERE / "task_prompt.txt").read_text(encoding="utf-8").strip()

# EUR/1K (input, output) — aus backend/app/llm/constants.py (2026-07-12).
PRICING = {
    "moonshotai/kimi-k2.7-code": (0.00074, 0.00350),
    "deepseek/deepseek-v4-pro": (0.00043, 0.00087),
    "deepseek/deepseek-v4-flash": (0.00009, 0.00018),
    "qwen/qwen3-coder": (0.00022, 0.00180),
    "z-ai/glm-5.2": (0.00091, 0.00286),
    "anthropic/claude-sonnet-5": (0.00200, 0.01000),
    "google/gemini-3.1-pro-preview": (0.00215, 0.01292),
    "google/gemini-3.5-flash": (0.00129, 0.00775),
    "qwen3.5-9b": (0.0, 0.0),
    "openai/gpt-5.6-luna": (0.001, 0.006),      # $1/$6 pro 1M (OpenRouter 2026-07)
    "openai/gpt-5.6-terra": (0.0025, 0.015),    # $2.5/$15 pro 1M
    "anthropic/claude-haiku-4.5": (0.001, 0.005),  # $1/$5 pro 1M
    "qwen/qwen3.6-27b": (0.000285, 0.0024),             # dense 27B (Timeline-Hype 2026-07)
    "qwen/qwen3.6-35b-a3b": (0.00014, 0.001),           # MoE 35B/3A
    "qwen/qwen3-coder-next": (0.00011, 0.0008),         # coder MoE, Reasoning nicht schaltbar
    "qwen/qwen3-next-80b-a3b-instruct": (0.00009, 0.0011),  # MoE 80B/3A instruct
}


def chat_full(base, key, model, prompt, extra, retries=3, timeout=300):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    payload.update(extra)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                         data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"chat scheiterte: {last}")


def load_arm(spec):
    return "\n\n".join((ARMS_DIR / p).read_text(encoding="utf-8").strip() for p in spec.split("+"))


def arm_prompt(arm_text):
    return ((arm_text + "\n\n---\n\n" if arm_text else "")
            + "Ignoriere jeglichen Kontext AUSSERHALB dieser Nachricht — folge nur der oben "
              "beschriebenen Methode/den Regeln (falls vorhanden) und der Aufgabe.\n\n" + TASK_PROMPT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--api-key-file", default="",
                    help="Pfad zu einer Datei mit dem API-Key (z.B. ein Docker-Secret). "
                         "Hat Vorrang vor --api-key-env; der Wert wird nie geloggt.")
    ap.add_argument("--mode", choices=["or", "llama"], required=True)
    ap.add_argument("--thinking", choices=["on", "off"], default="on")
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.api_key_file:
        key = Path(a.api_key_file).read_text(encoding="utf-8").strip()
    elif a.api_key_env == "NONE":
        key = ""
    else:
        key = os.environ.get(a.api_key_env, "")
    arms = [("baseline", ""), ("grundregeln", load_arm("grundregeln_v2.md")),
            ("regeln+workflow", load_arm("method.md+grundregeln_v2.md"))]
    think = a.thinking == "on"
    if a.mode == "or":
        extra = {"max_tokens": 32000, "reasoning": {"effort": "high"} if think else {"enabled": False}}
    else:
        extra = {"max_tokens": 8000, "chat_template_kwargs": {"enable_thinking": think}}
    if a.max_tokens > 0:
        extra["max_tokens"] = a.max_tokens
    pin, pout = PRICING.get(a.model, (0.0, 0.0))

    cells = []
    for arm_name, arm_text in arms:
        print(f"[{a.label}/{a.thinking}] {arm_name}", file=sys.stderr)
        try:
            t0 = time.perf_counter()
            d = chat_full(a.base_url, key, a.model, arm_prompt(arm_text), extra,
                          retries=a.retries, timeout=a.timeout)
            latency_s = round(time.perf_counter() - t0, 2)   # Wall-Time des Calls (inkl. Retries)
        except RuntimeError as e:
            print(f"  kaputt: {e}", file=sys.stderr)
            continue
        if not d.get("choices"):   # HTTP-200 mit Fehler-Body (z.B. Moderation) -> Zelle skippen, Lauf laeuft weiter
            print(f"  kein 'choices' (Fehler-Body): {str(d)[:200]}", file=sys.stderr)
            continue
        ch = d["choices"][0]
        msg = ch.get("message") or {}
        u = d.get("usage") or {}
        pt = u.get("prompt_tokens", 0) or 0
        ct = u.get("completion_tokens", 0) or 0
        tok_s = round(ct / latency_s, 1) if latency_s > 0 else None  # Output-Token/s
        cells.append({
            "model": a.label, "arm": arm_name, "thinking": a.thinking,
            "content": msg.get("content") or "",
            "reasoning": msg.get("reasoning") or msg.get("reasoning_content") or "",
            "finish_reason": ch.get("finish_reason"),
            "prompt_tokens": pt, "completion_tokens": ct,
            "cost_eur": round((pt * pin + ct * pout) / 1000, 6),
            "latency_s": latency_s, "tokens_per_s": tok_s,
        })
    tot = round(sum(c["cost_eur"] for c in cells), 5)
    Path(a.out).write_text(json.dumps(
        {"label": a.label, "model": a.model, "thinking": a.thinking, "total_cost_eur": tot, "cells": cells},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{a.label}/{a.thinking}: {len(cells)}/3 Arme, {tot} EUR -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
