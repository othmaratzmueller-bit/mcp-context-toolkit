# Changelog

Notable changes to **mcp-context-toolkit**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0-rc1] — 2026-06-29

First public release candidate.

### Added
- **Rules engine** — file-scoped rules (glob match) with priority ordering,
  stable fingerprint, JSON bundle output, and a fallback-markdown writer.
- **Memory recall** — keyword + frecency (hot/cold) ranking across two tiers
  (project / user), with a relevance floor that drops weak near-misses.
- **MCP server** (stdio) + **`context-toolkit-query` CLI** — rules query /
  validate / export, memory recall, user-tier dump, Context Studio export.
- **Context Studio viewer** — browse rules + memory, pending tab, content drilldown.
- **Auto-injection hook recipes** — per-file rules, session-start user memory,
  prompt-based recall (with `--exclude` dedup).
- **Usage sidecar** — per-machine `_usage.json` frecency with POSIX advisory
  locking; degrades to no-lock on non-POSIX platforms (e.g. Windows).

### Security / hardening
- Injected rules and memories are framed as **reference context, not commands**
  (previously imperative "INSTRUCTION TO ASSISTANT" phrasing). The assistant is
  told to weigh them as data and verify file/flag names against live code.
- **Per-body cap** on the always-loaded user-tier dump (`_MAX_BODY_CHARS`) so a
  single large memory file can't blow up the injected context; full body stays
  reachable via `get_memory(name)`.
- **Trust model documented** in the README (local/trusted stdio, not a secret
  store, content-is-context).

### Notes
- Everything under `examples/` is an **inert starter pack** — copy into your own
  store to activate; auto-discovery never loads it.

[1.0.0-rc1]: https://github.com/othmaratzmueller-bit/mcp-context-toolkit/releases/tag/v1.0.0-rc1
