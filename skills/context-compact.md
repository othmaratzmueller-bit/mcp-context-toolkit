---
name: context-compact
description: Compress verbose memory bodies + the hot index — trim prose, never facts/detail/correction-history. Shows a JSON diff for selection, applies only after approval, then git. Net smaller, never additive. Part of the context-toolkit (context-dream = orchestrator).
---

# /context-compact — slim bodies + index

Cuts fat, not meat: long descriptions/bodies to the essential, the `MEMORY.md`
hot index to short TOC lines. **Every memory/pointer is kept** — only prose gets
shorter. Pattern: **read → propose → PREVIEW → select → apply → git.**

## Prerequisites
- Toolkit MCP / engine CLI. `$CONTEXT_MEMORY_DIR` git-tracked.
- **Truncation guard:** if `MEMORY.md` is over budget (partially loaded), `Read` the
  whole file first — never operate on the truncated context copy.

## Flow
1. **Candidates** (read-only): verbose bodies (body ≫ needed), index hooks > ~80 chars.
2. **Compressed version as `diff.json`** (common Context Studio envelope — schema
   `../frontend/README.md`): per entry `op:"compact"`, `id`, `before`, `after`, `saved_bytes`.
   - Index hook: description to ~80 chars, keep title + `[[links]]` + recall keywords.
   - Body: remove only true redundancy.
3. **PREVIEW + select**: user takes/corrects per entry.
4. **Apply** (after approval) → regenerate index + catalog.
5. **Verify + git**: pointer set identical pre/post (nothing lost), store size ≤ before.

## Guardrails (hard)
- **Net SMALLER, never larger.** Grows → wrong → revert.
- **Never cut facts, technical detail, or correction history** — when in doubt, keep
  (field names, IDs, file:line stay verbatim).
- No pointer loss. Git is the safety net.

— part of [mcp-context-toolkit](https://github.com/othmaratzmueller-bit/mcp-context-toolkit) · by Othmar Atzmüller
