#!/usr/bin/env bash
# Voller deterministischer Sweep: 16 Cloud-Modelle (OpenRouter) + lokaler 9B (llama-server
# :11437, wie im alten codequality/sweep.sh) x 3 Arme (nackt/regeln/regeln+workflow) x
# Thinking (on immer; off nur wo abschaltbar). 4 Tasks je Config. Generierung -> out/sweep/;
# gejudged wird danach GRATIS + deterministisch via grade_output.py.
# Kosten-Deckel: --max-tokens 6000 (cloud). Key via secrets/openrouter.txt (nie geloggt).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "$HERE"
# Key-Datei via Env konfigurierbar — kein Repo-/User-spezifischer Hardcode (Leak-Audit 2026-07-17).
KEY="${OPENROUTER_KEY_FILE:?Set OPENROUTER_KEY_FILE to a file containing your OpenRouter API key}"
OR="https://openrouter.ai/api/v1"
OUT="$HERE/out/sweep"; mkdir -p "$OUT"
RUN="python3 $HERE/run_tasks.py"
MAXTOK=6000
MAX=8
gate() { while [ "$(jobs -r | wc -l)" -ge "$MAX" ]; do sleep 2; done; }

# label|slug|toggleable(1=Reasoning abschaltbar, 0=erzwungen)
CLOUD=(
  "glm-5.2|z-ai/glm-5.2|1"
  "deepseek-v4-pro|deepseek/deepseek-v4-pro|1"
  "deepseek-v4-flash|deepseek/deepseek-v4-flash|1"
  "qwen3-coder|qwen/qwen3-coder|1"
  "qwen3-coder-next|qwen/qwen3-coder-next|0"
  "qwen3.6-27b|qwen/qwen3.6-27b|1"
  "qwen3.6-35b-a3b|qwen/qwen3.6-35b-a3b|1"
  "qwen3-next-80b|qwen/qwen3-next-80b-a3b-instruct|0"
  "kimi-k2.7-code|moonshotai/kimi-k2.7-code|0"
  "gemini-3.1-pro|google/gemini-3.1-pro-preview|0"
  "gemini-3.5-flash|google/gemini-3.5-flash|0"
  "sonnet-5|anthropic/claude-sonnet-5|1"
  "haiku-4.5|anthropic/claude-haiku-4.5|1"
  "gpt-5.6-luna|openai/gpt-5.6-luna|1"
  "gpt-5.6-terra|openai/gpt-5.6-terra|1"
  "gpt-5.6-sol|openai/gpt-5.6-sol|1"
)
ARMS=("nackt|nackt" "regeln|regeln" "regeln+workflow|regelnwf")

launch() { # label slug arm armtok thinking mode base extra...
  local label="$1" slug="$2" arm="$3" armtok="$4" think="$5" mode="$6" base="$7"; shift 7
  $RUN --label "$label" --model "$slug" --base-url "$base" --mode "$mode" --thinking "$think" \
       --arm "$arm" --max-tokens "$MAXTOK" "$@" \
       --out "$OUT/${label}__${armtok}__${think}.json" > "$OUT/${label}__${armtok}__${think}.log" 2>&1
}

for m in "${CLOUD[@]}"; do
  IFS='|' read -r label slug tog <<< "$m"
  for a in "${ARMS[@]}"; do
    IFS='|' read -r arm armtok <<< "$a"
    launch "$label" "$slug" "$arm" "$armtok" on or "$OR" --api-key-file "$KEY" & gate
    if [ "$tog" = "1" ]; then
      launch "$label" "$slug" "$arm" "$armtok" off or "$OR" --api-key-file "$KEY" & gate
    fi
  done
done

# Lokaler 9B (:11437, kein Key). Thinking-on loopt bewusst -> robustness-Achse faengt es.
if [ "${SKIP_LOCAL:-0}" != "1" ]; then
  for a in "${ARMS[@]}"; do
    IFS='|' read -r arm armtok <<< "$a"
    for think in on off; do
      launch "qwen3.5-9b" "qwen3.5-9b" "$arm" "$armtok" "$think" llama "http://localhost:11437/v1" \
             --api-key-env NONE --timeout 300 --max-tokens 4000 & gate
    done
  done
fi
wait
echo "=== SWEEP KOMPLETT ==="
ls -1 "$OUT"/*.json 2>/dev/null | wc -l | xargs echo "  Config-Dateien:"
