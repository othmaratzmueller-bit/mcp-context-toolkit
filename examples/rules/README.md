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
