#!/usr/bin/env bash
# Code-Quality-Eval-Sweep: alle Modelle × 3 Arme × Thinking (an immer; aus nur wo das
# Modell Reasoning abschalten kann). OpenRouter-Subjekte + lokales 9B (llama-server).
# Ergebnis: out/<label>_<thinking>.json (volle Antwort + Reasoning + Kosten).
# SKIP_LOCAL=1  -> das lokale 9B auslassen (z.B. wenn llama-server gerade belegt ist).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
: "${OPENROUTER_API_KEY:?bitte OPENROUTER_API_KEY setzen}"
OUT="$HERE/out"; mkdir -p "$OUT"
OR="https://openrouter.ai/api/v1"
RUN="python3 $HERE/run_codequality.py"

# label|slug|toggleable(1=Reasoning abschaltbar, 0=erzwungen)
MODELS=(
  "sonnet-5|anthropic/claude-sonnet-5|1"
  "kimi-k2.7-code|moonshotai/kimi-k2.7-code|0"
  "deepseek-v4-pro|deepseek/deepseek-v4-pro|1"
  "deepseek-v4-flash|deepseek/deepseek-v4-flash|1"
  "glm-5.2|z-ai/glm-5.2|1"
  "qwen3-coder|qwen/qwen3-coder|1"
  "gemini-3.1-pro|google/gemini-3.1-pro-preview|0"
  "gemini-3.5-flash|google/gemini-3.5-flash|0"
)
MAX=3
gate() { while [ "$(jobs -r | wc -l)" -ge "$MAX" ]; do sleep 3; done; }

for m in "${MODELS[@]}"; do
  IFS='|' read -r label slug tog <<< "$m"
  $RUN --label "$label" --model "$slug" --base-url "$OR" --mode or --thinking on \
       --out "$OUT/${label}_on.json" > "$OUT/${label}_on.log" 2>&1 & gate
  if [ "$tog" = "1" ]; then
    $RUN --label "$label" --model "$slug" --base-url "$OR" --mode or --thinking off \
         --out "$OUT/${label}_off.json" > "$OUT/${label}_off.log" 2>&1 & gate
  fi
done

if [ "${SKIP_LOCAL:-0}" != "1" ]; then
  # Lokales 9B: grosszuegiges Budget + langer Timeout (Loop-Bound, siehe Reward-Hack-Eval).
  $RUN --label qwen3.5-9b-lokal --model qwen3.5-9b --base-url http://localhost:11437/v1 \
       --api-key-env NONE --mode llama --thinking on --max-tokens 16000 --timeout 650 --retries 1 \
       --out "$OUT/qwen3.5-9b-lokal_on.json" > "$OUT/qwen3.5-9b-lokal_on.log" 2>&1 & gate
  $RUN --label qwen3.5-9b-lokal --model qwen3.5-9b --base-url http://localhost:11437/v1 \
       --api-key-env NONE --mode llama --thinking off --max-tokens 16000 --timeout 650 --retries 1 \
       --out "$OUT/qwen3.5-9b-lokal_off.json" > "$OUT/qwen3.5-9b-lokal_off.log" 2>&1 & gate
fi
wait
echo "=== CODE-QUALITY-SWEEP KOMPLETT ==="
ls -1 "$OUT"/*.json 2>/dev/null | wc -l | xargs echo "  Ergebnis-Dateien:"