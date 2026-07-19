# Context Studio

A single-file, zero-dependency, **offline** viewer for the mcp-context-toolkit.
By Othmar Atzmüller · MIT.

The viewer itself ships **inside the package** at
`src/mcp_context_toolkit/viewer/index.html` (so it travels with `pip install`).
This folder documents it and the JSON contract it reads.

Two modes:

- **Review** — the *preview-gate*. Load a curation skill's `diff.json`, accept or
  reject each proposal, and export a `decision.json` (a plain Blob download — no
  backend, no network). The skill then applies only the accepted items.
- **Browse** — read-only view of your store: `memory.json` (packages by tier with
  frecency heat) and `rules.json` (rules by category and priority).

## Generate a snapshot

```bash
context-toolkit-query --export-studio ./studio       # rules.json + memory.json + viewer
cd studio && python3 -m http.server                  # then open http://localhost:8000
```

`--export-studio` writes `rules.json`, `memory.json` (if a memory store is found),
and copies `index.html`. If the memory dir also holds a pending-decision ledger
(`_DREAM_PENDING.md`) and/or a preview-gate diff (`_PENDING_DIFF.json`, the canonical
path all curation skills write to), those are copied too as `pending.md` and
`diff.json` — a host embedding the viewer (e.g. an editor extension) can inject them
straight into `window.CONTEXT_DATA` without a manual file picker. Memory dir resolves
from `--memory-dir` → `CONTEXT_MEMORY_DIR` → `.claude/memory` auto-discovery.

## Three ways the viewer finds data (in order)

1. `window.CONTEXT_DATA = { diff, memory, rules }` — injected by a host that
   embeds the viewer (e.g. a `data.js` loaded before it).
2. `fetch('./memory.json' | './rules.json' | './diff.json')` — when served over
   http (the `--export-studio` + `http.server` path).
3. **Drag-and-drop** — open `index.html` directly off disk (`file://`, where
   `fetch` is blocked) and drop a `diff.json` onto the Review tab.

## Host hooks — on-demand content (optional)

Two **optional** function hooks on `window.CONTEXT_DATA` let a host serve content the
metadata snapshot deliberately omits. Both are absent in pure offline use (the viewer
degrades gracefully — the body section is hidden, the Pending tab shows a hint):

- `fetchBody(name) -> Promise<string>` — full body of a memory package, loaded when
  you expand it in **Browse** (the snapshot's `memory.json` carries metadata only, no
  bodies). A host typically wires this to an auth-gated endpoint that reads the store
  from disk. Alternatively inject a `body` string per package.
- `fetchPending() -> Promise<string>` — the consolidation flag-ledger (the persistent
  "human decides" backlog a pass writes but does not auto-resolve), shown in the
  **Pending** tab. Alternatively inject a `pending` string.

Both return **plain text** (markdown is fine — it is rendered as text, never parsed to
HTML, keeping the XSS-safe contract). Example host wiring (`*-data.js` loaded before the
viewer):

```js
window.CONTEXT_DATA = {
  memory: /* metadata snapshot */, rules: /* … */,
  fetchBody: name => fetch(`/your/auth/endpoint/memory/${encodeURIComponent(name)}`)
                       .then(r => r.json()).then(d => d.content),
  fetchPending: () => fetch(`/your/auth/endpoint/pending`).then(r => r.json()).then(d => d.content),
};
```

## The diff.json contract (preview-gate)

One **common envelope** for every curation skill, so the viewer is op-agnostic:

```jsonc
{
  "kind": "context-studio/diff",
  "tool": "context-dream",            // which skill produced this
  "store": ".claude/memory",          // optional, informational
  "summary": "12 proposals · net −4.2 KB",
  "items": [
    {
      "id": "feedback_x",             // stable key = the memory's name (REQUIRED)
      "op": "prune",                  // prune|compact|merge|reclassify|rename|reorder|flag|link_fix|normalize
      "title": "feedback_x",          // display title (defaults to id)
      "detail": "superseded by …",    // human-readable reason / what changes
      "before": "…", "after": "…",    // optional — shown as a 2-col diff (compact/merge)
      "saved_bytes": 1234,            // optional — net bytes (− shrinks, + grows)
      "from": "misc/x.md", "to": "feedback/y.md",  // optional — shown as from → to (move/rename/classify)
      "evidence": "…",                // optional — shown as a quote block (prune/flag)
      "default": true                 // optional — pre-checked accept (default true)
    }
  ]
}
```

The viewer renders whichever fields are present — a skill only fills what its op
needs. `op` just picks the colour of the badge.

## The decision.json contract (export)

```jsonc
{
  "kind": "context-studio/decision",
  "tool": "context-dream",
  "accepted": ["feedback_x", "…"],    // ids the user kept
  "rejected": ["…"]                   // ids the user unchecked
}
```

A skill reads `decision.json` and applies only `accepted`, then commits.

## memory.json / rules.json (Browse)

Produced by `--export-studio`; see `examples/frontend/` for trimmed samples and
the exact field set. Both are read-only — Browse never mutates a store.

## Safety

All data is rendered with `textContent` / `createElement`, never `innerHTML` —
the viewer dogfoods the toolkit's own `escape_user_data_in_html` rule, so a
memory body or rule example containing markup is shown as text, never executed.
