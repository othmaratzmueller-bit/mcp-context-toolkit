---
name: context-classify
description: Route atomic memories into thematic packages + tiers (core/user/project) — builds/updates the bundle map. Proposes a JSON diff, shows it for selection, applies only after approval, then git. Part of the context-toolkit (context-dream = orchestrator).
---

# /context-classify — route memories into packages + tiers

Turns a sprawl of atomic memory files into a **bundled** structure: tight thematic
packages + tier assignment (`core` = always-on hot, `user` = cross-project,
`project` = repo-specific). Shrinks the hot index structurally.
Pattern (all memory skills): **read → propose → PREVIEW → select → apply → git.**

## Prerequisites
- Toolkit MCP (`list_memories`/`recall`) or engine CLI. `$CONTEXT_MEMORY_DIR` git-tracked.

## Flow
1. **Inventory** (read-only): every memory's frontmatter (name, type, description).
   Capture naming drift (sentence-vs-slug) here too.
2. **Propose a bundle map**: ~40-60 tight packages, EVERY memory in exactly ONE,
   tier per package. (Large stores: a panel of clusterings → synthesise → verify.)
3. **PREVIEW as `diff.json`** in the common Context Studio envelope (`{tool,
   summary, items:[…]}` — schema: `../frontend/README.md`): one item per memory,
   `op:"reclassify"` (or `"rename"`), `id`, `from`, `to`. Show it — touch no data.
4. **Select**: user takes all / part / corrects (`decision.json`).
5. **Apply** (after approval): merge members into package files as `## slug` sections
   (body **verbatim**, lossless), regenerate index + catalog.
6. **Verify + git**: every memory assigned exactly once, nothing lost. Commit the store.

## Guardrails
- **Gated**: no data mutation without approval (step 4). Default = proposal only.
- **Match by filename slug**, not the frontmatter `name` (which may be a verbose title).
- Bundle tightly (recall returns whole packages) — never mix unrelated themes.
- Net smaller index, never additive.

— part of [mcp-context-toolkit](https://github.com/othmaratzmueller-bit/mcp-context-toolkit) · by Othmar Atzmüller
