# Dependency graph — example index

An illustrative `reference-index.json`. Drop a file like this at
`<store>/graph/reference-index.json` and every per-file query *also* returns that
file's coupling — what it imports and what imports it — so an agent sees the blast
radius before it edits.

## Format

A flat map. Keys are `py:<dotted.module>` or `js:<path>`; each value is
`{ "imports": [...], "imported_by": [...] }` referencing other keys:

```json
{
  "py:app.services.pipeline": { "imports": ["py:app.services.auth"], "imported_by": ["py:app.api.routes"] }
}
```

A queried file matches the entry whose key is a **suffix** of its path — so
`app/services/pipeline.py` matches `py:app.services.pipeline`, and `ui/dashboard.js`
matches `js:ui/dashboard.js`.

## You generate it; the engine only reads it

The toolkit ships **no** graph builder — it stays language-agnostic. Produce
`reference-index.json` however fits your stack (a static-import scan, your bundler's
module metadata, `tree-sitter`, …) and refresh it as part of your normal indexing.
No graph file — or no matching entry — just means `dependencies` comes back empty;
the feature is purely additive.

## Inert

Like the other examples, nothing here auto-loads. This file only illustrates the
format; your real graph lives in your store's own `graph/` dir.
