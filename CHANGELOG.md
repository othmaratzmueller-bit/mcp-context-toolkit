# Changelog

Notable changes to **mcp-context-toolkit**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [1.0.0-rc6] — 2026-07-21

### Added
- **Backlink-aware recall ranking.** `MemoryEngine.recall()` gains an optional
  `backlink_boost` parameter ({name: factor} map) that adds a small additive term per
  inbound `[[link]]`, so structurally important knowledge (memories cited by many
  others) rises without needing explicit usage. The MCP server computes the map from
  the resolved edge graph as `log1p(inbound_count) * 0.1` — log-damped so it never
  dominates the keyword/frecency base score, and ranking is unchanged when the map
  is empty.
- **New MCP tool `memory_dream_status(files_threshold=3, lint_threshold=2)`.**
  Reports whether a `/context-dream` consolidation pass is due: counts memory files
  changed in the last 7 days (mtime heuristic), sums `memory_lint` issues
  (broken links + orphans + stale pointers), and returns a three-level recommendation
  (`🧠 dream fällig` / `💡 dream empfohlen` / `✅ kein dringender Bedarf`) with
  configurable thresholds.

### Fixed
- **`memory_dream_status` cleanup.** Moved the inline `import datetime` to the module
  head, removed a duplicated `MEMORY.md` skip-list entry, replaced the private
  `memory_reloader._watch` access with a public `watch_dirs` property, and documented
  that "since last dream" is a stateless 7-day mtime heuristic (not a tracked
  last-run timestamp) to close the doc-vs-code gap.
- **Lint regressions.** Resolved 4 ruff findings (duplicate `import datetime`,
  ambiguous `l` variable names in `memory.py`/`test_indexer.py`, unused
  `expected_boost` in the backlink test). CI lint step is now binding
  (`continue-on-error` removed).
- **CI Python matrix.** Added `3.13` (the actual runtime) alongside `3.11`/`3.12`.

### Changed
- **README: generic store-convention defaults.** The auto-discovery description now
  documents `.context`/`.claude` as the generic defaults and names `.talos` only as an
  example of a `CONTEXT_STORE_CONVENTIONS` override (it is not a code default —
  `_DEFAULT_STORE_CONVENTIONS = (".context", ".claude")`). Previously the README
  implied a `.talos`-first default that only holds inside a container with
  `CONTEXT_STORE_CONVENTIONS=.talos,.context,.claude` set.

### Local store repairs (not in the tracked release)
The following were fixed in the *maintainer's local workspace* only — `.context/` and
`.talos/` are git-excluded (local stores, never pushed), so they do not ship with the
release. They are recorded here for the maintainer's own history and for anyone who
re-creates a project store from scratch:
- **Project rules failed schema validation (`rule_count: 0`).** All 10 GR1–GR6 rule
  files in the local `.context/rules/` were missing the required `created` date field,
  so `validate_rules` reported `ok: false` and the server loaded *no* rules at runtime.
  Added `created: 2026-07-11` to each file; `validate` now passes with 10 rules loaded.
  (The shipped `examples/rules/` were already correct — 8/8 have `created`.)
- **Memory store root.** The local project memory tier was moved to `.talos/memory/`
  (set `CONTEXT_MEMORY_DIR=.talos/memory` or `CONTEXT_STORE_CONVENTIONS=.talos,.context,.claude`).
  The `.context/memory/` index was a stale placeholder pointing at a non-resolving
  relative path; it is now cleaned and documents the active root. `memory_lint`
  reports no stale pointers on either root.

## [1.0.0-rc5] — 2026-07-19

### Added
- **`--export-studio` now also copies the preview-gate ledger/diff.** If the memory dir
  holds `_DREAM_PENDING.md` (the `/dream` consolidation ledger) and/or the canonical
  `_PENDING_DIFF.json` (the preview-gate output all curation skills write to), `_cmd_export_studio`
  copies them into the output dir as `pending.md` / `diff.json` alongside `rules.json`/
  `memory.json`/`index.html`. Lets an embedding host (e.g. an editor extension) inject
  both into `window.CONTEXT_DATA` and auto-load them without a manual file picker.

## [1.0.0-rc4] — 2026-07-15

### Added
- **Embedding-host support in the rules payload.** `_rules_payload()` (CLI) now emits a
  per-rule `source_path` (the YAML file the rule was loaded from) and `tier`, plus a
  top-level `skipped` list of schema-invalid files. Lets a host embedding the toolkit
  (e.g. an editor extension) open the underlying rule file, badge its tier, and flag
  invalid files without re-implementing the engine's own loader/validator.
- **Project rules tier is now optional on the CLI.** `--export-studio`, `--validate`,
  and plain query now run shared-only when a workspace has no `.context/rules` of its
  own but a shared org tier (`CONTEXT_SHARED_RULES_DIR`) exists, instead of hard-failing
  with exit 2. `_load_all_rule_tiers`/`_cmd_validate`/`_cmd_write_fallback`/
  `_cmd_export_studio` now accept `Path | None` for `rules_dir`. Mirrors the MCP server's
  existing project-optional wiring and is what lets an embedding host (e.g. the VS Code
  extension) show the shared grundregeln in any fresh workspace, not only the toolkit repo.

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
- **One invalid rule file no longer aborts the whole load on the serving path.**
  `RulesEngine` gained a `load_errors` property that accumulates tier-prefixed
  skip reasons from a lenient (`strict=False`) `load_directory` call, mirroring
  the `MemoryEngine`'s existing lenient behaviour. `strict=True` (the default,
  used by CI/`validate_rules()`) is unchanged — it still raises on the first
  bad file.

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
