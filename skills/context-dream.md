---
name: context-dream
description: Consolidate and clean a markdown memory store — merge duplicates, compress verbose entries AND the index, repair [[links]], normalise names. Net SMALLER, never additive (like sleep consolidating the day). Always git-backed/revertible. The orchestrator of the context-toolkit memory skills.
---

# /context-dream — consolidate the store ("sleep")

Turns a grown, raw memory store back into a curated one: merge dupes, compress,
repair `[[links]]`, normalise names — **without losing detail or correction
history**. The counterpart to fast `push` capture: raw in, then `dream` cleans it.

## Core principle — net smaller, never accumulate
`/context-dream` makes the store **smaller + cleaner, never additive-larger.** It
does NOT auto-add an index pointer for every orphan — orphans are deliberately
recall-only; pointer-bloat would defeat the lean hot index. Hard invariant: after
a run, `MEMORY.md` ≤ before.

## Prerequisites
- The memory store (`$CONTEXT_MEMORY_DIR`) is git-tracked → every run is a
  revertible commit. Not in git → **abort**.
- Toolkit MCP with memory tools (`memory_lint`, `recall`, `memory_usage`) or the
  `context-toolkit-query`/engine CLI as fallback.
- **Truncation guard:** if `MEMORY.md` exceeds the load budget (partially loaded),
  `Read` the whole file first — never operate on the truncated context copy.

## Pipeline (orchestrates the other skills)
1. **`context-classify`** — route memories into thematic packages + tiers.
2. **`context-prune`** — flag stale / superseded / redundant (never auto-delete).
3. **`context-compact`** — compress verbose bodies + the index hooks.
4. **Reindex** — regenerate `MEMORY.md` (lean package TOC) + `_descriptions.md`
   (member→package catalog) from the sources (build artifacts, never hand-edited).
5. **Verify + commit** — `memory_lint` clean, store size ≤ before, no member lost.

## Preview-gate
The sub-skills' proposals combine into one `diff.json` in the common Context
Studio envelope (`{tool, summary, items:[{id, op, …}]}` — schema:
`../frontend/README.md`). Review it in the terminal or the Context Studio viewer,
keep what you want, and apply only the accepted `decision.json` items before the
commit. Nothing mutates the store until you approve.

## Guardrails (hard)
- **Net smaller, never larger.** Grows → wrong → revert.
- **Never delete without a flag**; never cut correction history or technical detail.
- Never operate on a truncated index. Git is the safety net.

— part of [mcp-context-toolkit](https://github.com/othmaratzmueller-bit/mcp-context-toolkit) · by Othmar Atzmüller
