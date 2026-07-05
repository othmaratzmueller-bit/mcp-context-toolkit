# Decisions — example ADRs

Architectural Decision Records: *why* the code is built the way it is. Same query
model as rules — `applies_to.files` globs — so opening a matching file surfaces the
decisions that shaped it, right next to the rules that govern it.

```bash
cp -r examples/decisions/* /path/to/your/.context/decisions/
```

## What's in here

Two linked records showing the lifecycle:

| File | key | status |
| --- | --- | --- |
| `2026-01-01_layered_config.yaml` | `layered_config` | `superseded` |
| `2026-02-15_config_via_pydantic_settings.yaml` | `config_via_pydantic_settings` | `accepted`, `supersedes: layered_config` |

A later decision points back with `supersedes: <key>` and the older one flips to
`status: superseded` — the record stays (history matters), it's just no longer the
active guidance.

## Schema

`key` (slug), `title`, `date`, `status` (`draft | accepted | rejected | superseded |
deprecated`), `applies_to.files` (globs), `reason` (the decision + why + rejected
alternatives). See either file, or the top-level `README.md` → "Store format".

## Inert

Auto-discovery never picks up anything under `examples/` — copy these into your own
`.context/decisions/` to make them active.
