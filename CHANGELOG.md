# Changelog

Notable changes to **mcp-context-toolkit**. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.0.0] — 2026-09-24

### Added — recall-learning evidence lifecycle

- **Closed scout comparison and maintenance preview.**
  `complete_scout_comparison` records an idempotent, resumable comparison
  transaction and learns only bridge terms that pass the production-ranking
  replay. Its durable claim lets a retry reconcile an interrupted target
  without duplicate tags. `preview_learned_tag_ablation` then reads those
  accepted, claimed comparisons and replays each current `learned_tag` with
  and without the tag. It reports `necessary`, `candidate`, or
  `not_decidable`, and marks a group only when the individually candidate tags
  also remain redundant together. The preview never changes a memory file.

- **Fail-closed required learning lock.** A curated recall-learning write now
  rejects the target when the platform cannot provide the required exclusive
  file lock; optional usage telemetry keeps its best-effort behaviour.

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

### Changed — recall ranking

- **Unicode tokenizer.** `core._WORD_RE` is now `\w+` instead of
  `[a-z0-9_]+`, for memory, work, docs and domain search alike. The ASCII
  class indexed German as fragments ("lügt" → `l` + `gt`, "übersetzen" →
  `bersetzen`). This reshuffles rankings well beyond umlaut queries, because
  record lengths and IDF shift with it: on 150 real logged recall queries
  against the memory store at measurement time (~130 records), 29 kept an
  identical top-6, 50 the same top-6 set, and the mean Jaccard overlap was
  0.74. Against the only reference available (six scout-found memories from
  logged comparison runs), top-6 coverage went from 5/6 to 6/6. Work and
  docs search change as well; the same 150 queries (memory prompts, so only
  a proxy for real work/docs lookups) kept an identical top-6 in 69 (work) /
  57 (docs, compared per file) cases, the same top-6 set in 85 / 74, mean
  Jaccard 0.79 for both. Domain search was not measured.
  `prefix_slice` now uses U+10FFFF as its upper sentinel (U+FFFD cut off
  letters beyond the BMP).
- **NFC normalisation** (`core.normalise_text`) before tokenising, for index
  and query alike: `\w` does not match a combining mark, so decomposed text
  (macOS file names, some PDF extracts) split "lügt" into `lu` + `gt`.
  Recall-learning bridge terms are compared and written in NFC as well.
- **Bundler: member tags become package `tags`.** `render_package` now writes
  the case-insensitive union of its members' tags into the package
  frontmatter — the unnormalised recall field — instead of only into the
  body, where a large package's length diluted them (measured: a 3000-word
  package fell below the relevance floor with the tag in the body only, and
  is recalled with it as a package tag). `tags` is rebuilt from the current
  members on every bundle, so a departed member's tags leave with it;
  `learned_tags` is carried over from the existing package (plus the
  members' own), so nothing learned is lost.
- **`build_bm25_index` accepts a third field element `length_normalised`**
  (default True). `False` is BM25F's per-field `b = 0`: hits go to
  `flat_postings`, are not divided by record length and do not add to it.
  Memory `tags` use it. A record without such hits is scored by the unchanged
  expression, so an index without flat fields ranks bit for bit as before;
  in the memory store all scores still move, because tags no longer count
  toward record length and document frequency now includes tag hits.

### Added

- **Recall learning (`learn_recall_gap`, MCP tool + `--learn-recall-gap` CLI).**
  A scout-found recall gap is persisted as bridge terms in the target memory's
  `learned_tags` — a new frontmatter list, indexed together with `tags` in the
  same BM25F tag field and kept apart only so its provenance survives
  bundling — but only after a shadow replay through the production
  ranking (same frozen frecency snapshot, same backlink boost, no session
  exclude) proves the target then ranks within `limit`, and only the minimal
  effective subset. Member slugs resolve to their package, as in `get_memory`.
  Writes are locked, re-read under the lock, edit only the `learned_tags` node, and
  are verified by re-parse before an atomic replace; anything else is rejected
  fail-closed. Every outcome logs a `recall_learning` event;
  `RecallEventLog.SCHEMA_VERSION` is now 4. CLI exit 0 accepted/noop, 2 rejected.
  New public helpers: `core.query_terms`, `MemoryEngine.with_memory`,
  `usage.exclusive_file_lock` (UsageStore's lock now uses it).

  Before the tag field was made length-independent, a replay over a real
  132-record store rejected three member-slug gaps in large packages as
  ineffective. The gate correctly rejected those attempted writes; the
  later tag-field change addresses that package-length dilution.

  Trust boundary: every word of a bridge term must literally be a word of the
  query (checked on whitespace words rather than ranker tokens), and the
  file is re-read right before the replace so an unlocked host edit during
  the replays is never overwritten.

- **Curated memory-context candidate lane.** Memory frontmatter may now carry
  optional `kontexte.themen` and `kontexte.taetigkeiten` alongside existing
  `bausteine`. The `recall` MCP tool accepts all three axes as optional OR-sets:
  they can fill at most two unused result slots but never alter text ranking or
  replace a keyword hit. A matching observed Baustein is more specific than a
  topic/activity label and therefore exclusively selects the delivery candidates
  for this lane; the broad matches remain inspectable. The CLI now delegates to
  the same path as MCP (including exclusion backfill and read-only frecency
  boosts) via repeatable `--baustein`, `--thema`, and `--taetigkeit` flags —
  this also applies the backlink boost the CLI's own prior scorer omitted, so
  the CLI/prompt-hook lane's keyword ranking can shift slightly from its
  pre-merge behaviour, independent of any context axis being supplied.
  `memory_context_candidates` exposes the raw matches, while
  `memory_recall_explain` returns each delivered candidate's keyword and/or
  context provenance without warming frecency; the normal `recall` delivery
  path does warm frecency for a context-delivered memory exactly as for a
  keyword hit — "delivered" is "used" there, matching the existing recall
  contract, not an oversight. Per-session recall events now
  preserve that provenance and the uncapped context-candidate denominator. A
  versioned, SHA-256-fingerprinted Golden corpus replays owner prompt forms
  through the real engine and CLI adapters, checks their equality, and fails on
  corpus drift.

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

## [1.0.0] — 2026-08-21

First stable release. Consolidates the work since rc9. The "Known gaps
going into 1.x" section below is the authoritative list of what is still
experimental or TALOS-tied — nothing else in this repo claims otherwise.

### Added
- **`context-toolkit-query --doctor`** — reports discovered store paths
  (rules/memory/work/docs), loaded record counts, and load errors in one
  call, without starting the MCP server. (This toolkit's CLI is
  flag-based throughout, not subcommand-based — `--doctor`, not
  `doctor`.)
- **`context-toolkit-query --validate-all`** — validates rules, memory,
  and work stores together with a single exit code, for CI use.
- **`context-toolkit-query --migrate-decisions --from DIR --to DIR
  [--dry-run]`** — migrates a legacy `.context/decisions/` store into the
  Work Store (`kind: decision`). The underlying function existed since
  rc9 but was never wired to argparse or tested — fixed together with a
  real bug the new tests caught: `--dry-run` was still creating the
  target directory (not writing files into it, but not a no-op either).
- **English Work Store field aliases** (`models.py`, `AliasChoices`) —
  `reuse`/`searched_for`/`found`/`conclusion`/`uses`/`extends`/`creates`/
  `modules`/`requires_owner`/`unblocked_by`/`migrated` are now accepted
  alongside the original German field names on read. Unknown keys are
  still a load error either way.
- **`WorkTheme` as a validated slug** instead of a fixed literal — any
  syntactically valid theme is now accepted; a host may still constrain
  the list via configuration.
- **`examples/generic-project/`** — a complete, TALOS-free example
  covering rules, memory, and work-store records.
- **Local usage telemetry** (`usage.py::TelemetryStore`) — optional,
  fail-soft JSONL event log (tool, latency, matched count, delivered
  tokens, estimated token savings) under `.context/logs/`.
- **Generic domain manifest parsing** (`domain.py::DomainManifest`,
  `DomainEngine.discover_domains/list_domains/get_domain`) — reads
  `.context/domains/<id>/domain.yaml` and can list/look up domains
  programmatically.

  **Now wired to real MCP tools** — `list_domains()`/`get_domain(id)`/
  `query_domain(id, query, limit?)` (see README's "Domains" section).
  `DomainFilter` gained `types`/`priorities` (the Rules vocabulary,
  alongside the existing `themes`/`statuses` for Work) and, like the rest
  of this engine's YAML models, now rejects unknown manifest keys instead
  of silently dropping them. The shipped `examples/generic-project/`
  domain manifest previously did not even parse against this model
  (wrong field names entirely — `domain:`/`triggers:` instead of
  `id:`/`trigger:`); it is now a real, tested `critical_security`-style
  rules view. Current scope is Ausbaustufe 1 (manifest discovery + these
  three tools) — no `domain` CLI subcommand yet, no automatic
  path/prompt-based routing; a domain is always queried explicitly by id.

### Changed
- Decision loader failures are reported via the existing `load_errors`
  channel (`[decision]` prefix) instead of being silently dropped.
- **Public Python API and MCP responses are now English-first.**
  `WorkEngine.for_file()` (was `fuer_datei()`), `WorkEdges.searched()`
  (was `gesucht_worden()`), and the `WorkEngine.lint()` /
  `work_lint` MCP tool result keys `reuse_not_checked` (was
  `reuse_ungeprueft`) and `broken_reuse_refs` (was `kanten_gebrochen`,
  whose per-entry fields are now `kind`/`target`/`state`/`hint` instead
  of `typ`/`ziel`/`zustand`/`hinweis`) are the primary names. The old
  German method/function names remain as thin deprecated wrappers for
  existing Python callers; MCP tool responses use the English names
  only — there is no transition alias at that layer, unlike the YAML
  loader.
- README's Work Store schema example and prose now show the English
  field names as the reference; the old German names are called out
  once as a documented read-only alias, not shown as the primary form.
- **Formal host-adapter protocol** (`host_adapters.py`, new module) —
  `HostAdapter` is a `typing.Protocol` with two methods (`resolve_symbol`,
  `path_to_id`); the previously private, `mcp_server.py`-internal TALOS
  resolver logic (`_kanten_check_factory`/`_path_to_id_factory`) is now
  `TalosDiscoveryAdapter`, one formal implementation of that protocol —
  still built in (not yet split out to `examples/host-profiles/talos/`,
  see remaining gaps below), but callable and testable through the
  Protocol instead of two private functions. `DemoAdapter` ships
  alongside it as a minimal second implementation, proving the protocol
  needs nothing TALOS-specific. `tests/host_adapters/test_talos_adapter.py`
  now goes through `TalosDiscoveryAdapter.path_to_id` rather than
  importing a private function from `mcp_server.py`.
- **`mcp_server.py` split into `server/`** — one module per domain
  (`app.py` wiring + `reloaders.py` + `serializers.py` + `discovery.py` +
  `tools_rules.py`/`tools_memory.py`/`tools_work.py`/`tools_docs.py`/
  `tools_domains.py`/`tools_baustein.py`/`tools_capability.py`), each
  exposing a `register(tool_decorator, ...)` function `build_server()`
  calls. A change to `tools_docs.py` now needs no edit to `tools_work.py`
  or any other domain module. `mcp_server.py` itself becomes a thin
  backward-compatible re-export — the `context-toolkit-mcp` console
  script (`pyproject.toml`) still resolves `mcp_context_toolkit.
  mcp_server:main`, and every existing `from mcp_context_toolkit.
  mcp_server import ...` keeps working at the same import path. No
  public MCP tool name, tool count, schema, or return value changed —
  the full test suite (335 tests) passes byte-for-byte the same before
  and after. 17 of 24 tool docstrings shifted by one indent level as a
  mechanical side effect of moving each `@tool` out of `build_server()`
  into its own module-level function; whitespace-normalized, all 17
  are unchanged text.
- **`--migrate-decisions` corrected against a real legacy store, not just
  its own synthetic fixtures — twice.** First pass, checked against the
  DoD from the plan (field mapping, conflict/overwrite reporting,
  non-transferable-field reporting), found three bugs: the target Work
  Card `key` was taken from the filename STEM, not the source's own
  `key:` field; `status` was hard-coded to `done` for every card
  regardless of the source's own
  `draft/accepted/rejected/superseded/deprecated` value; `theme` was
  hard-coded to `"engine"`. A fresh-context review of that first pass then
  found the fix itself was incomplete: it sanitized only LEADING invalid
  characters, so the single most common ADR filename shape
  (`adr-007-cache`, no digit prefix to strip) still produced an
  unloadable key while reporting `"ok": true` — the exact bug class the
  etappe claimed to have closed — and a source-supplied `key:` field
  passed through with no validation at all, the same hole from the other
  side. Fixed for real: `_sanitize_key()` now replaces every character
  outside `[a-z0-9_]` (not just a leading run), applied unconditionally
  to BOTH the source `key:` field and the filename-stem fallback, with
  a hard assertion against `WorkItem.key`'s own
  `^[a-z][a-z0-9_]*$` pattern; `--theme` is validated at parse time
  against `WorkTheme`'s own `_validate_theme_slug` (imported, not
  re-derived as a second pattern that could quietly diverge from it) so
  an invalid theme fails loud before any card is written instead of
  writing successfully and failing to load later. A THIRD, fresh-context
  review of that second pass then found two more real bugs, both proven
  via `WorkEngine.lint()` against the round's own repro cases rather than
  argued from reading: `supersedes` was written through verbatim while
  every target `key` went through `_sanitize_key()` — a same-run
  reference to a decision whose key needed sanitizing (`adr-001-old` →
  `adr_001_old`) pointed at a key that no longer existed, `lint()`
  reporting a `dangling_refs` entry the migration itself had just
  created; and `closed:` was written unconditionally even after the
  status-default fix started producing `open` cards, so a migrated draft
  carried a closed-date on an open card, `lint()` reporting an
  `unclosed_pairs` entry for exactly the input (drafts, typo'd statuses)
  the earlier fix was built for. Fixed: `supersedes` now runs through
  the same `_sanitize_key()` as the target key; `closed:` is only
  written when the mapped status is one Work considers closed
  (`done`/`dropped`/`superseded`, via `work.py`'s own `_CLOSED_STATUSES`,
  not a hand-copied set). Also corrected in the same round: `migrated`/
  `targets` now count distinct target keys instead of source files (a
  same-run collision no longer inflates the count past what actually
  landed on disk), and `overwritten` is computed from a snapshot of the
  target directory taken BEFORE the run starts, so a same-run collision
  is reported only under `conflicts`, never misreported as
  "overwritten" (a claim that specifically means "existed before this
  run") — `conflicts` itself is reported in `--dry-run` too, which
  previously couldn't see a same-run collision at all since it only
  checked "does the target already exist on disk"; an unrecognized
  source status now defaults to `open` (not `done`) and is reported per
  item under `unmapped_status`; a rejected/deprecated card keeps a
  one-line "migrated from legacy status: …" note in its body so that
  distinction survives the merge into Work's shared `dropped` state;
  `supersedes` is mapped instead of reported as an unmappable dropped
  field; a non-dict top-level YAML document is now skipped with a clear
  stderr message instead of crashing the whole run.
  Any source field with no Work Card equivalent (e.g.
  `applies_to.modules`) is still reported per item under `dropped_fields`
  rather than vanishing without a trace, and a repeated run still reports
  which targets it `overwritten`. The worked example under
  `examples/migrations/` (`before/` → `after/`) now includes a third
  source (a draft decision `supersedes`-ing an already-migrated one
  through a deliberately non-slug reference) so the corpus can actually
  exercise both round-three fixes, not just load without error; the
  drift-guard test asserts both `WorkEngine.from_directory(after,
  strict=True)` loads cleanly AND `lint()` reports empty
  `dangling_refs`/`unclosed_pairs` — a strict load alone missed both
  round-three bugs, since a syntactically valid but dangling reference
  and a half-closed card both still parse.
- **Every `limit:` parameter across rules/memory/work/docs now shares one
  clamp (`core.clamp_limit`) instead of four independent `[:limit]`
  slices.** `docs.recall()` was the only one already guarded (an
  unclamped negative value is a valid, reversed Python slice — measured
  incident: `limit=-1` returned 1,954 sections, ~1.44M tokens, for a
  single call). `memory.recall()`, `work.recall()`,
  `WorkEngine.for_file()`/`fuer_datei()`, `query_domain()`'s rules/work
  branches (which build their own local BM25 index and slice manually,
  bypassing the engine `.recall()` methods), `memory_usage()`, and
  `capability.Wissensindex.suche()` (`find_capability`'s backing index)
  all had the identical gap — a second, third, fourth, fifth
  independently-written copy of the same bug waiting to happen, not four
  to six domain-specific concerns. Fixed by moving the clamp into
  `core.py` (`MAX_RECALL_LIMIT = 50`, `clamp_limit()`) and having every
  site call it; `docs.py`'s own local `_MAX_LIMIT` constant is removed in
  favor of the shared one. A malformed type (e.g. a string) is rejected
  by FastMCP's own schema validation before any tool body runs — that
  layer was already correct and needed no change; this closes the
  numeric-range gap FastMCP's type check does not cover. A fresh-context
  review of this pass found a latent contract break it introduced: the
  CLI's per-prompt memory recall deliberately over-fetches
  (`limit + len(exclude)`, where `exclude` is the hook's growing
  already-injected set — measured up to 106 entries on real session-state
  files) so that filtering `exclude` back out still leaves `limit`
  results; the new default 50-item ceiling would have silently truncated
  the candidate pool before that filter ran once a session's `exclude`
  set grew past it, under-filling the response with no error. Fixed:
  `MemoryEngine.recall()` gained an explicit `max_limit` override (a
  caller-specific ceiling, not a way around the tool-surface default),
  and the CLI passes its own over-fetch target through it.
- **`list_work` with no theme/status/kind filter returns a per-theme
  topic overview instead of every item — for BOTH the plain default
  call and `include_archived=True`.** This is the one call shape
  `clamp_limit` cannot help with — it takes no `limit` at all — and it
  had no natural size bound: measured against the real TALOS work store,
  the plain default (live backlog, 1272 items) serialized to ~366K
  characters (~91K tokens), and `include_archived=True` (5124 items) to
  ~326K tokens — larger than the docs_recall incident that started this
  whole etappe, and the plain default is the single most likely call
  shape of the tool. A fresh-context review measured the 91K-token
  default case and flagged that a decision against capping it had been
  made without that number in front of it; scoping the fix to just
  `include_archived=True` was reconsidered and widened to cover both.
  The overview groups by `theme` — the same field a follow-up
  `list_work(theme=<name>)` filters on — with a hint pointing at that
  follow-up call. `include_archived` still controls whether the overview
  itself counts the live backlog or the full archive; in the live-backlog
  case (`include_archived=False`) the `done`/`dropped`/`superseded` keys
  are OMITTED from each topic entirely rather than reported as `0` — a
  second review pass measured every topic showing zeroes there even for
  themes with hundreds of closed items (152 `done` items in one theme,
  filtered out before counting), which reads as "no completed work exists" and is
  false; omitting the keys makes the scope visible in the response shape
  itself, not only in the tool's docstring. Measured on the real store:
  365,825 to 2,329 characters for the plain default, 325,732 to 2,375
  for `include_archived=True`.
- **Every `list_work` item-list response (filtered or not) now shares one
  response budget (`_LIST_WORK_MAX_CHARS = 60_000`), truncated with a
  `truncated`/`omitted` marker once exceeded.** The claim above that "a
  single theme/status/kind filter is naturally small" did not hold: a
  third review pass measured `status="done"` alone at ~926K characters
  (~231K tokens) on the real TALOS work store — bigger than either
  unfiltered case this etappe already capped, and the single most
  plausible way to route around the new topic-overview default (a
  documented tool parameter, not an edge case). `kind`/`status` values
  cut this store into a handful of large slices, not many small ones;
  only `theme` narrows reliably, and even the largest theme
  (272 items) sits at ~20K tokens. Modeled on
  `_DOC_SNIPPET_CHARS`, which already bounds a single `docs_recall` hit
  independent of its source section's size — this binds the response
  independent of which filter (or none) produced it, current and future
  alike, rather than special-casing each call shape a measurement
  happens to find. Measured on the real store: `status="done"`
  925,621 to 56,470 characters (3812 to 195 items, `omitted: 3617`) —
  the shrink loop originally measured the unwrapped item array while
  returning the wrapped `{items, truncated, omitted, hint}` object,
  whose extra nesting and marker fields it never accounted for; a
  fresh-context review measured four call shapes landing over the
  60,000-character budget as a result (`kind="step"` at 63,389), fixed
  by having the loop measure the actual wrapped response it returns.

### Documentation

- **README gained a `## Stability` section** classifying every surface
  as `public`, `experimental`, or `host-specific` — the four built-in
  domains, the CLI, and the MCP tool surface are `public` (a breaking
  change gets a changelog entry and a version bump, never a silent
  shape change); [Custom Domains](README.md#domains) are `experimental`
  ("Ausbaustufe 1" — manifest shape and CLI scaffold may still change,
  now stated where the feature itself is documented, not only in the
  changelog); the `HostAdapter` Protocol is `public` but concrete
  adapters (`TalosDiscoveryAdapter`) are `host-specific`.
- **New `## Architecture` section** with a Mermaid diagram: Files →
  Loader → Engines → Reloader → MCP Tools/CLI, with the content-write
  boundary marked explicitly — a human or agent editing content
  directly is the only content-write path; your rule/memory/work/docs
  **content** is never created, edited, or deleted by the engine. A
  fresh-context review caught the first draft's wording ("the engines
  never write... the ONLY write path") as false and contradicting the
  README's own existing [What it writes to disk](README.md#what-it-writes-to-disk)
  section 500 lines below it: `RulesEngine.write_fallback_markdown`
  writes on every server start with a project rules tier loaded
  (`server/app.py`'s startup path), and the memory store's frecency
  usage sidecar writes on every `recall` call (`usage.py`) — work/docs/
  domains have no such sidecar (`work_recall`'s own docstring: "no
  frecency/backlink boost, unlike memory"). The claim is now scoped to
  content, matching the
  qualifier that section already used correctly — and that section
  gained the one row it was missing (`write_fallback_markdown`'s
  target), an old gap the new diagram made acute by claiming
  completeness next to it.
- **New Glossary** (under [Work store](README.md#work-store)): Work
  Store, Reuse Record, Evidence, Rollup — short definitions for terms
  used throughout the README without a single reference point before
  this.
- **`CONTRIBUTING.md` gained a Release checklist**: fresh clone + fresh
  venv, `ruff`/`pytest` clean with zero TALOS-conditioned skips, build
  both wheel and sdist and install from a throwaway venv, walk the
  README's own examples without TALOS context, cross-check
  `pyproject.toml`'s version against the CHANGELOG's newest entry and
  the git tag before creating it. The same review found step 1's plain
  `pip install -e ".[dev]"` insufficient — the `dev` extra
  (`pyproject.toml`) omits `ruff` and `build`, which steps 2 and 3 both
  need; CI (`ci.yml`) installs them separately and pins `ruff==0.16.0`
  on purpose (a linter defines by version what counts as an error).
  Step 1 now installs the exact line CI uses instead of a narrower one
  that cannot run the checklist's own next two steps.

### Known gaps going into 1.x (tracked, not yet closed)
- No `profiles/generic.yaml` / `profiles/talos.yaml` split — TALOS
  conventions are not yet isolated behind an explicit profile.
- `TalosDiscoveryAdapter` still ships inside the core package rather
  than moving to `examples/host-profiles/talos/` — doing so needs a way
  for a running TALOS session to point the server at an external
  adapter file, which is a separate, riskier change than the protocol
  extraction itself (see `host_adapters.py`'s module docstring).

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
