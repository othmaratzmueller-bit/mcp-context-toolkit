# Context Studio — sample data

Trimmed samples so you can see the viewer with zero setup. The viewer itself is
`../../src/mcp_context_toolkit/viewer/index.html`; the full contract is in
`../../frontend/README.md`.

- `diff.json` — a preview-gate proposal covering every op (prune, compact, merge,
  reclassify, rename, flag). Drop it onto the **Review** tab.
- `memory.json` / `rules.json` — tiny **Browse** snapshots.

Quickest look (drag-and-drop, no server):

```bash
# open the bundled viewer directly, then drag diff.json onto the Review tab
xdg-open ../../src/mcp_context_toolkit/viewer/index.html   # or: open … (macOS)
```

Served (enables Browse auto-load too):

```bash
cp ../../src/mcp_context_toolkit/viewer/index.html .
python3 -m http.server      # then open http://localhost:8000
```
