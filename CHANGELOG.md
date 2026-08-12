# Changelog

Notable changes to **mcp-context-toolkit**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed — BREAKING for rule and decision authors

- **Unknown keys in a rule or decision YAML are now a load error**
  (`models.py::_StrengesModell`, `extra="forbid"` on all `Rule*`/`Decision*`
  models). A file that loaded yesterday can raise today.

  This is the same lesson as rc9's `kanten:` fix, one layer over: there, the
  loader read fields by name and unknown ones never reached the model, so the
  check had to live in the loader. Here the loader builds with `Rule(**data)`,
  so `extra="forbid"` catches it at the point where the file is read.

  Measured on a probe rule carrying two typos (`exclude` instead of `excludes`,
  plus an invented top-level key): it loaded without complaint, and the
  exclusion was gone. In the host project that would have meant a
  `non_negotiable` licence rule silently applying to ingested third-party
  repositories.

  **Migration:** a rule that needs to carry extra metadata has `tags:
  list[str]` for it — that is the intended overflow. Everything else is a typo
  by definition now. Run the loader once over your store; the errors name the
  offending key.

- **The decision loader no longer swallows failures.** Its body ended in
  `except Exception: pass`, so a broken decision file vanished without a trace.
  Measured over a 397-file store: three were affected and had been invisible
  for weeks — two with an unterminated quoted scalar, one carrying a field the
  schema did not know.

  This had to change *together* with `extra="forbid"`: with a swallowing
  loader, a typo'd key would no longer have cost a FIELD but the whole RECORD,
  and still silently. The fix would have made the diagnosis worse.

  Failures now go into the existing `load_errors` channel with a `[decision]`
  prefix. The loader stays lenient by design — one broken file must not take
  the store down — it is now lenient *and* loud. Callers that render
  `load_errors` should no longer label the count "rule file(s)".

### Added

- **`Decision.amended`** (`date | None`) — when a decision was refined IN
  PLACE, as opposed to `supersedes`, which points at a different record that
  replaces it. The field had been written in a real store since 2026-07-22 and
  discarded silently ever since; it was adopted rather than deleted, because
  the author meant it and the schema was the part that was wrong.

### Fixed

- **YAML mappings with non-string keys no longer crash the load.** `2026-08-03:`
  parses as `datetime.date`, `on:`/`yes:` as `bool` — and `Decision(**data)`
  raises `TypeError: keywords must be strings` *before* pydantic sees anything,
  slipping past `except ValidationError`. Measured: the error escaped through
  `load_directory` and the MCP server's reloader, taking down the entire rule
  load over one malformed decision file. Non-string keys are now reported like
  any other bad file.

## [1.0.0-rc9] — 2026-08-11

### Added
- **`kanten.gefunden` and `kanten.entscheidung`** (`models.py::WorkEdges`,
  `work.py`). The reuse record is a three-step proof — what was searched for,
  what turned up, what was concluded — but only the first step had a field.
  `gefunden` is deliberately separate from `nutzt`/`erweitert`/`baut` because of
  the **rejected** hit: "found the mixin pattern, discarded it — no class here"
  fits none of the three, and it is the most valuable part of the record, since
  it proves the search was not only run but weighed. Without a home for it the
  honest answer has to be dropped or dressed up as something it isn't.
  `gefunden` counts towards `gesucht_worden()` (a hit list is the result of a
  search); `entscheidung` deliberately does **not** — it is free text and thus
  the cheapest field here to fill without doing anything, and letting it open
  the gate would hand back the loophole `kanten: {}` was closed for.

### Changed
- **Unknown keys under `kanten:` are now a load error.** Previously the loader
  read the five known fields by name and everything else vanished without a
  trace: no error, no lint hit, no warning. An `extra="forbid"` on the model
  would not have helped — unknown keys never reached it. Found in a live store
  where six records had spelled out the three-step proof verbatim and silently
  lost two thirds of it. The allowed names are derived from the model, not kept
  in a second hand-maintained list that would go stale at the next field.

  **Upgrade note:** a store containing typo'd or invented `kanten` keys will now
  surface those records in `load_errors` instead of loading them half-empty.
  That is the point — but it is a behaviour change for anyone relying on the
  silent tolerance.

- **`kanten.entscheidung` must be a string.** A list or mapping there is a load
  error, not a silent `str()`. Previously `entscheidung: [a, b]` became the
  character sequence `"['a', 'b']"` — a Python repr that gets served as evidence
  and reads like a sentence without being one. New `_als_text` is the counterpart
  to `_als_liste` and refuses non-strings for the same reason that one refuses
  non-lists.

  **Upgrade note:** this is stricter than rc9's first cut, which also accepted a
  number (`entscheidung: 42` became `"42"`). Any record whose `entscheidung` is
  not a string now lands in `load_errors`. Multiple points belong in one
  sentence — that is what the field is for.

- **`__version__` now comes from the installation metadata** (`importlib.
  metadata`) instead of a second hard-coded string. There is one source of truth
  again: `pyproject.toml`. The two had drifted apart unnoticed (`rc6` in the code
  against `rc8` in the project file).

  **Upgrade note:** the version is now read from the INSTALLED distribution. Bump
  `pyproject.toml` without re-running `pip install -e .` and the package keeps
  reporting the old number — the failure mode moved from a visible second string
  to invisible metadata. `TestVersion` in `tests/test_engine.py` couples the two
  and fails loudly in exactly that case; it is the reason this trade is
  acceptable. Importing from a source tree without any installation yields
  `0.0.0+unbekannt` rather than an invented number.

## [1.0.0-rc8] — 2026-08-05

### Added
- **Per-file work-item delivery.** `WorkEngine.fuer_datei(file_path, limit=8)`
  (`work.py`) surfaces the open/active work items recorded for a given file —
  matched via `kanten.nutzt/erweitert/baut` (forward, symbol-level) first, then
  `evidence.files` of still-open records, capped at 8. The CLI's `_offene_arbeit()`
  and the MCP server's `query_rules_for_file` tool both expose it as a fourth
  section alongside rules/decisions/dependencies (`offene_arbeit`), fail-soft when
  the work store is absent or fails to load. `fingerprint_rules()` gains an
  optional third `arbeit` parameter and folds each item's key/status/title into
  the dedup hash — otherwise the live-reload dedup would silently suppress a new
  open item whenever only the work store changed and the rule set did not.

### Changed
- **`discover_work_dir()` unified.** Moved from a private duplicate in
  `mcp_server.py` to a single public function in `core.py` (`.resolve()`d, since
  callers derive sibling paths from it). The CLI had grown its own second copy
  that differed only in the missing `.resolve()`; both call sites now share one
  implementation.

## [1.0.0-rc7] — 2026-07-28

### Added
- **Work-item content store.** A third content store alongside rules and memory:
  `WorkItem`/`WorkKind` (`goal`/`step`/`note`)/`WorkStatus`
  (`open`/`active`/`done`/`dropped`/`superseded`)/`WorkTheme`/`WorkEvidence` models
  (`models.py`) and a new `WorkEngine` (`work.py`, read-only: `get`/`list`/`children`/
  `rollup`/`lint`/`recall`). `rollup()` computes a goal's x/y child-status summary live
  from the records themselves (never stored, so it cannot drift) and reports `active`
  as its own count rather than folding it into `open`. `lint()` mirrors `memory.py`'s
  shape (`dangling_refs`, `closeable_goals`, `missing_evidence`, `unclosed_pairs`,
  `load_errors`) so both stores are consumed the same way. Six new MCP tools:
  `work_recall`, `get_work`, `list_work`, `work_children`, `work_lint`, `work_status`.
- **BM25F ranking extracted to `core.py`.** `build_bm25_index`/`bm25_scores`/
  `prefix_slice`/`split_token` moved out of `memory.py` (pure relocation, no behaviour
  change) so `WorkEngine.recall()` reuses the exact same, already-reviewed algorithm
  instead of duplicating it. `memory.py`'s field-weight constants changed from `int` to
  `float` to match the now-shared `fields_by_key: dict[str, list[tuple[str, float]]]`
  signature (a dict value type is invariant in Pyright, so ints would not have widened
  at the call site).

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
