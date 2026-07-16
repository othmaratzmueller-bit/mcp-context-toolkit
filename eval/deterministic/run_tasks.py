#!/usr/bin/env python3
"""Generierungs-Adapter: stellt EINEM Modell alle deterministischen Tasks und
speichert die Roh-Antworten im Format, das grade_output.py erwartet. KEIN Judge hier
— gejudged wird lokal + deterministisch (harness.py). Die einzige Kosten-Quelle ist
diese Generierung; das Grading ist gratis und beliebig oft wiederholbar.

Reuse des OpenRouter-/llama-Aufrufmusters aus ../codequality/run_codequality.py
(Key via --api-key-file, nie geloggt; Thinking on/off via reasoning bzw.
enable_thinking). Optional 'Arme' (nackt / +Regeln / +Regeln+Workflow), damit die
'haelt sich an Regeln'-Dimension erhalten bleibt.

Usage (Beispiel — bewusst NICHT im Repo ausgefuehrt, kostet API):
  python3 run_tasks.py --label qwen3.6-27b --model qwen/qwen3.6-27b \\
    --base-url https://openrouter.ai/api/v1 --mode or --thinking off \\
    --api-key-file /pfad/secrets/openrouter.txt --arm nackt --out out/qwen3.6-27b_off.json
"""
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

from tasks import TASKS  # noqa: E402

ARMS_DIR = HERE.parent / "arms"

# EUR/1K (input, output) — identisch zu ../codequality/run_codequality.py (2026-07-12).
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
    "openai/gpt-5.6-luna": (0.001, 0.006),
    "openai/gpt-5.6-terra": (0.0025, 0.015),
    "openai/gpt-5.6-sol": (0.005, 0.030),
    "anthropic/claude-haiku-4.5": (0.001, 0.005),
    "qwen/qwen3.6-27b": (0.000285, 0.0024),
    "qwen/qwen3.6-35b-a3b": (0.00014, 0.001),
    "qwen/qwen3-coder-next": (0.00011, 0.0008),
    "qwen/qwen3-next-80b-a3b-instruct": (0.00009, 0.0011),
}


def chat_full(base, key, model, prompt, extra, retries=3, timeout=300):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    payload.update(extra)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                         data=json.dumps(payload).encode(), headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"chat scheiterte: {last}")


def load_arm(arm: str) -> str:
    """'nackt' -> '', sonst die Arm-Textbloecke aus ../arms/ (reuse grundregeln/method)."""
    if arm == "nackt":
        return ""
    spec = {"regeln": "grundregeln_v2.md",
            "regeln+workflow": "method.md+grundregeln_v2.md"}.get(arm, "")
    if not spec:
        return ""
    return "\n\n".join((ARMS_DIR / p).read_text(encoding="utf-8").strip() for p in spec.split("+"))


def build_prompt(arm_text: str, task_prompt: str) -> str:
    return ((arm_text + "\n\n---\n\n" if arm_text else "")
            + "Ignoriere jeglichen Kontext AUSSERHALB dieser Nachricht.\n\n" + task_prompt)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key-file", default="", help="Datei mit dem Key (nie geloggt)")
    ap.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    ap.add_argument("--mode", choices=["or", "llama"], required=True)
    ap.add_argument("--thinking", choices=["on", "off"], default="on")
    ap.add_argument("--arm", choices=["nackt", "regeln", "regeln+workflow"], default="nackt")
    ap.add_argument("--max-tokens", type=int, default=0)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import os
    if a.api_key_file:
        key = Path(a.api_key_file).read_text(encoding="utf-8").strip()
    elif a.api_key_env == "NONE":
        key = ""
    else:
        key = os.environ.get(a.api_key_env, "")

    think = a.thinking == "on"
    if a.mode == "or":
        extra = {"max_tokens": 16000, "reasoning": {"effort": "high"} if think else {"enabled": False}}
    else:
        extra = {"max_tokens": 8000, "chat_template_kwargs": {"enable_thinking": think}}
    if a.max_tokens > 0:
        extra["max_tokens"] = a.max_tokens
    pin, pout = PRICING.get(a.model, (0.0, 0.0))
    arm_text = load_arm(a.arm)

    answers = {}
    for name, task in TASKS.items():
        print(f"[{a.label}/{a.thinking}/{a.arm}] {name}", file=sys.stderr)
        prompt = build_prompt(arm_text, task.prompt)
        try:
            d = chat_full(a.base_url, key, a.model, prompt, extra, timeout=a.timeout)
        except RuntimeError as e:
            print(f"  kaputt: {e}", file=sys.stderr)
            answers[name] = {"content": "", "finish_reason": "error", "completion_tokens": 0, "cost_eur": 0.0}
            continue
        if not d.get("choices"):
            answers[name] = {"content": "", "finish_reason": "no-choices", "completion_tokens": 0, "cost_eur": 0.0}
            continue
        ch = d["choices"][0]
        msg = ch.get("message") or {}
        u = d.get("usage") or {}
        pt, ct = u.get("prompt_tokens", 0) or 0, u.get("completion_tokens", 0) or 0
        answers[name] = {
            "content": msg.get("content") or "",
            "finish_reason": ch.get("finish_reason"),
            "completion_tokens": ct,
            "cost_eur": round((pt * pin + ct * pout) / 1000, 6),
        }

    out = {"model": a.label, "thinking": a.thinking, "arm": a.arm, "answers": answers}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{a.label}/{a.thinking}/{a.arm}: {len(answers)} Tasks -> {a.out}")
    print("Jetzt gratis + deterministisch graden:  python3 grade_output.py " + a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
