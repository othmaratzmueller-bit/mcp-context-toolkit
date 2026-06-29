# Rules — starter pack ("Grundpaket")

A small, generic, **ready-to-copy** rule set for starting from zero. Copy this
directory's contents into your project's rule dir (or point `CONTEXT_RULES_DIR`
here) and adapt. These are sensible defaults — not exhaustive; real projects
extend and override them.

```bash
cp -r examples/rules/* /path/to/your/.context/rules/
```

## What's in here

| Category | Rules |
| --- | --- |
| `security/` | `no_hardcoded_secrets` (SEC1), `no_dynamic_code_eval` (SEC2) |
| `code_quality/` | `function_length_limit` (CQ1), `no_dead_code` (CQ2), `constants_over_magic_values` (CQ3) |
| `frontend/` | `no_inline_event_handlers` (FE1), `escape_user_data_in_html` (FE2) |
| `workflow/` | `verify_after_change` (WF1) |

## How a rule works

Each `.yaml` declares `applies_to.files` globs; the engine returns only rules
whose globs match the file you're touching, sorted by `priority`
(`non_negotiable` > `mandatory` > `recommended`). See any file here for the full
schema, or the top-level `README.md` → "Store format".

Edit the globs (`applies_to.files`) to match your layout — the defaults assume a
`src/**` tree. Drop the categories you don't need.

## These examples are inert — they never auto-load

Auto-discovery looks for `<dir>/.context/rules` (or `<dir>/.claude/rules`) walking
up from the working directory. This starter pack lives under `examples/rules/`, so
it can **never** be picked up implicitly — nothing here becomes active until *you* copy it into
your own rules directory. If you point `CONTEXT_RULES_DIR` (or `--rules-dir`)
directly at this folder, the toolkit prints a loud `NOTE: loading EXAMPLE/starter
rules …` to stderr so the examples don't silently become your production rule set.
