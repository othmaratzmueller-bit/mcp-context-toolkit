---
name: context-prune
description: Find stale/superseded/redundant memories and propose removal — flag-first, NEVER auto-delete. Shows a JSON diff for selection, removes only after approval (git = net). Part of the context-toolkit (context-dream = orchestrator).
---

# /context-prune — retire stale / duplicate knowledge

Keeps the store honestly small: **superseded** (a newer dated memory replaces an
old one), **redundant** (near-duplicate), **obsolete** (refers to removed
features/old versions). Pattern: **read → propose → PREVIEW → select → apply → git.**
Conservative — when in doubt, keep.

## Prerequisites
- Toolkit MCP (`memory_lint`, `memory_usage`, `recall`) or engine CLI. Store git-tracked.

## Flow
1. **Gather signals** (read-only):
   - `memory_usage` → heat: **0 hits + old** = candidate (NOT decisive alone — heat is
     a lower bound; native auto-recall doesn't count).
   - dated `(superseded YYYY-MM-DD)` markers, "replaced by" notes.
   - near-duplicates (recall clusters with high overlap).
2. **Candidate list as `diff.json`** (common Context Studio envelope — schema
   `../frontend/README.md`): per entry `op:"prune"`, `id`, `detail` (reason), `evidence` — **flag only**.
3. **PREVIEW + select**: user decides per entry (remove / keep / merge).
4. **Apply** (after approval): `git rm`, or for ambiguous cases flag into a pending
   ledger instead of deleting. Regenerate index + catalog.
5. **git**: commit the store; diff shows a net reduction.

## Guardrails (hard)
- **NEVER auto-delete.** Default is FLAG; removal only on explicit selection.
- **Never discard correction history**: a dated correction beats the old state, but
  the learning path stays (superseded marker, not silent deletion).
- **Heat is a signal, not a verdict** — cold ≠ worthless.
- Git is the safety net.

— part of [mcp-context-toolkit](https://github.com/othmaratzmueller-bit/mcp-context-toolkit) · by Othmar Atzmüller
