# mcp-context-toolkit

> A generic [MCP](https://modelcontextprotocol.io) server that feeds any MCP client
> the *right context at the right time* — file-scoped **rules** and frecency-ranked
> **memory**, loaded on demand from plain markdown + YAML-frontmatter stores.

## The idea

Long-lived AI coding/agent sessions accumulate context: coding standards, security
rules, architectural decisions, hard-won lessons. Stuffing all of it into one
monolithic system prompt is wasteful and noisy — a frontend task should never carry
backend security rules, and a memory you haven't needed in weeks shouldn't crowd the
ones you use daily.

`mcp-context-toolkit` keeps that knowledge as a directory of small markdown files and
serves it over MCP so the client pulls only what's relevant:

- **Rules** match by file-path glob — open `api/users.py`, get back the security + db
  rules that apply to it, nothing else.
- **Memory** matches by relevance and **frecency** (frequency + recency) — recall
  surfaces the most-used, most-recently-used notes first.

Both are just markdown with frontmatter. Git is the storage, history and backup. The
engine is read-only; you (or a consolidation pass) own the writes.

## Two content types

| Type | Query model | Use it for |
| --- | --- | --- |
| **Rules** | file-path glob → matching rules, by priority | standards, security policies, review gates — anything tied to *which file you touch* |
| **Memory** | keyword relevance × frecency (hot/cold) | decisions, preferences, lessons — anything worth recalling later |

## Two tiers

Both content types load from multiple roots tagged by tier:

- **user** — cross-project knowledge (`~/...`), shared across everything you do
- **project** — locked to one repo

On a name collision the **project tier wins** (specific beats general). A single
`recall` spans both tiers, so a session sees its repo's notes *and* your global ones
in one ranked list.

## Hot / cold memory (frecency)

Every `recall` / `get_memory` hit is counted in a per-machine sidecar (`_usage.json`,
gitignored). The score is **frequency-dominant and log-damped** — it does *not* decay
with wall-clock time, so a weekend (or three-week) pause never cools a heavily-used
memory. `memory_usage` reports the hot→cold ranking; a consolidation step can use it
to surface hot notes first.

## Install

```bash
git clone https://github.com/othmaratzmueller-bit/mcp-context-toolkit
cd mcp-context-toolkit
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # optional
```

Zero heavy deps: Python stdlib + `pydantic`, `pyyaml`, `mcp`.

## Register with any MCP client

It's a stdio MCP server — works with any MCP-capable host (editors, agents, custom
clients). Point it at your stores via env:

```json
{
  "mcpServers": {
    "context": {
      "command": "context-toolkit-mcp",
      "env": {
        "CONTEXT_RULES_DIR": "/abs/path/to/repo/.context/rules",
        "CONTEXT_MEMORY_DIR": "/abs/path/to/repo/.context/memory",
        "CONTEXT_USER_MEMORY_DIR": "/home/you/.context/memory"
      }
    }
  }
}
```

Any unset dir is auto-discovered by walking up from the working directory. Memory tools
register only when a memory store is found — rules-only setups keep working untouched.

## Tools

**Rules**

| Tool | Purpose |
| --- | --- |
| `query_rules_for_file(file_path)` | rules whose globs match a path, by priority |
| `query_rules(type?, scope?, priority?, module?)` | bulk fetch by metadata |
| `get_rule(key)` | full body of one rule |
| `list_rule_keys(type?, scope?)` | enumerate keys |
| `validate_rules()` | dry-run validate the rule directory |

**Memory**

| Tool | Purpose |
| --- | --- |
| `recall(query, limit?)` | top memories across both tiers, frecency-ranked |
| `get_memory(name)` | full body + metadata of one memory |
| `list_memories(type?, tier?)` | enumerate, filterable |
| `memory_lint()` | hygiene: broken `[[links]]`, index orphans, stale pointers |
| `memory_usage(limit?)` | hot→cold usage report (opens, recalls, heat) |

## CLI

A second entry point, `context-toolkit-query`, exposes the rule engine on the command
line (used e.g. by editor hooks for per-file rule injection):

```bash
context-toolkit-query path/to/file.py --format bundle   # rules for one file
context-toolkit-query --validate                        # validate the rule set
```

## Store format

A rule (`.context/rules/**/*.yaml`):

```yaml
key: no_hardcoded_secrets
title: No hardcoded secrets
type: security            # security | workflow | code_quality | frontend | architecture | infrastructure | module
scope: backend            # backend | frontend | database | infrastructure | docs | all
priority: non_negotiable  # non_negotiable | mandatory | recommended
modules: [all]
applies_to:
  files: ["**/*.py", "**/*.js"]
summary: One-liner shown in query results.
content: |
  ## Full markdown body, fetched via get_rule.
created: 2026-01-01
```

A memory (`.context/memory/**/*.md`):

```markdown
---
name: prefer_composition
description: One-line summary used for recall ranking.
metadata:
  type: feedback          # user | feedback | project | reference | misc
tags: [design]
---

The note itself. Link related notes with [[their-name]].
```

`MEMORY.md` in the memory dir is the human-curated index (skipped as a record);
`memory_lint` checks it against the actual files.

## Design principles

- **Read-only engine.** It loads and ranks; it never mutates your stores.
- **Deterministic.** Keyword + frecency scoring, no LLM in the loop — same inputs, same order.
- **Degrade, don't crash.** A malformed file is skipped, not fatal; a missing/corrupt
  usage sidecar resets to empty.
- **Generic.** No assumptions about any specific client or project. Ships only example
  rules; real rule/memory sets live in the consuming repo.

## License

MIT — see `LICENSE`.
