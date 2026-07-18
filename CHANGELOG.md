# Changelog

Notable changes to **mcp-context-toolkit**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0-rc3] — 2026-07-11

### Added
- **Memory link graph.** `MemoryEngine.edges()` (resolved `(source, target)` pairs) and
  `backlinks(name)` (inbound edges) expose the `[[link]]` graph; `_descriptions.md` now
  carries a `← cited by:` suffix, and `get_memory` returns a `cited_by` list. Member-slug
  links are credited to the package that absorbed them; self-edges are dropped.
- **Context Studio `Graph` tab.** The exported viewer renders the directed link graph with a
  vendored [Cytoscape.js](https://js.cytoscape.org/) (MIT) — node colour = tier, size =
  frecency heat, click a node to open it. `memory.json` gained a resolved `edges` list, and
  `--export-studio` now also copies `cytoscape.min.js` (Graph tab degrades to a hint if absent).
- **OKF-compatible frontmatter fields** `resource` (asset URI) and `timestamp` (ISO 8601).
  Unquoted ISO datetimes are normalized to ISO 8601 (`Z` → `+00:00`).
- **Always-on working-method block.** `context-toolkit-query --method-block` prints a shipped
  `method/method_block.md` resource; wire it as a `UserPromptSubmit` hook for re-injection
  against instruction-decay (opt out with `CONTEXT_METHOD_BLOCK=0`).

### Fixed
- **Nested `metadata:` frontmatter.** `tier`, `members`, `tags` (and the new `resource`/
  `timestamp`) are now read from a nested `metadata:` block, not only top-level — matching the
  fallback `type` already had. Previously a package written with `metadata: { members: … }`
  parsed with **empty members**, silently disabling member-link resolution (and inflating
  `memory_lint` broken-link counts) on every such file. Top-level still wins over nested.
- **Stale `__version__` in `__init__.py`.** The package-level `__version__` was `"0.1.0"`
  instead of `"1.0.0rc3"`, causing `importlib.metadata.version()` and runtime version checks
  to report the wrong version.

### Changed
- `_memory_payload` sources members from the (now nested-aware) parser and drops the
  redundant top-level-only `_package_members` file re-read.

## [1.0.0-rc2] — 2026-07-05

### Fixed
- **`RulesEngine.query_decisions_for_file`** — removed dead/inverted `excludes`
  branch that read a `DecisionAppliesTo.excludes` attribute the model never
  defines (always fell through to `False`, so the loop body was unreachable
  and had no effect on the result). Matching now runs the include-glob check
  directly, appending each matching `Decision` once per file.

### Removed
- **`scripts/migrate_changelogs.py`** — dropped this one-off migration tool
  from the package. It hard-coded a specific repo's folder layout and has no
  use for generic consumers; git history retains it for anyone who needs it.

### Tests
- Added `test_query_decisions_for_file_matches_globs` covering include-glob
  matching and de-duplication of a decision with multiple matching patterns
  (93 tests, up from 92).

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
