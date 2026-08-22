# `--migrate-decisions` — a worked example

`before/` is a small legacy `.context/decisions/` store — three ADR-style
YAML files: one with a date-prefixed filename and a `key:` field, one
with neither, and one that `supersedes` the second through a deliberately
non-slug reference. `after/` is exactly what running the command below
produces (regenerate it yourself to check — the files here are not
hand-edited):

```bash
context-toolkit-query --migrate-decisions \
  --from before --to after --theme migrations
```

What to notice comparing the two directories:

- `2026-01-01_layered_config.yaml` → `layered_config.md` — the real `key:`
  field wins over the date-prefixed filename stem (a filename-derived key
  would violate `WorkItem.key`'s `^[a-z][a-z0-9_]*$` pattern and fail to
  load).
- `status: accepted` → `status: done`; `status: rejected` →
  `status: dropped`, with a `_Migrated from legacy status: rejected._`
  note kept in the body — never blanket-mapped to `done`, and the
  rejected/deprecated distinction survives the merge into `dropped`.
- `owner: platform-team` and `applies_to.modules: [config]` in the source
  have no Work Card equivalent — they show up in the command's JSON
  result under `dropped_fields`, not lost silently.
- `draft_caching_layer.yaml` → `draft_caching_layer.md` — `status: draft`
  maps to `status: open`, so no `closed:` date is written (an open card
  with a closed date is a lint violation the store flags on its own).
  Its `supersedes: per-module-env-reads` (hyphenated, not a valid key on
  its own) is sanitized the same way the target key is and correctly
  resolves to `per_module_env_reads.md`.
- All three migrated cards load cleanly through
  `WorkEngine.from_directory(after, strict=True)` AND `lint()` reports
  empty `dangling_refs`/`unclosed_pairs` — both asserted directly in
  `tests/test_cli.py::TestMigrateDecisions::test_examples_migrations_after_matches_a_fresh_run`,
  which also re-runs the migration and diffs `after/` byte-for-byte. The
  third source file exists specifically so those two `lint()` assertions
  have something to catch — an earlier, two-file version of this corpus
  had neither a `supersedes` reference nor an open-status card, so both
  assertions passed vacuously even while the underlying bugs they guard
  against were still present.
