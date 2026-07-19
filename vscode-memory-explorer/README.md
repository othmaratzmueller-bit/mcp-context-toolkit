# Memory Explorer VS Code Extension

Eine VS Code Extension für das MCP Context Toolkit — verwalt deine Memories direkt in VS Code.

## Features

- **Memory Explorer Sidebar** — Alle Memories in einer Tree View anzeigen
- **Hot/Cold Ranking** — Memories nach Frecency sortiert (Recalls, Opens, Last Access)
- **Inline-Edit** — Memories direkt im Editor bearbeiten
- **Context Menu** — Rechtsklick für Edit, Delete, Duplicate, Share
- **Command Palette** — `memory:recall`, `memory:list`, `memory:validate`
- **Real-time Updates** — Datei-Watcher für automatische Updates
- **Link Validation** — Prüfe `[[links]]` zwischen Memories

## Installation

### Manuell

1. Diese Extension in VS Code öffnen
2. `F5` drücken oder `Run and Debug` → `Extension Development: Run Extension`
3. Die Extension wird in einer neuen VS Code Instanz geladen

### Als Published Extension

```bash
npm install
npm run vsce package
npm run vsce publish
```

## Verwenden

### Sidebar öffnen

1. `View` → `Appearance` → `Memory Explorer` aktivieren
2. Oder: `Ctrl+Shift+P` → `Memory Explorer: Show Explorer`

### Memories bearbeiten

- **Doppelklick** auf eine Memory → öffnet im Editor
- **Rechtsklick** → `Edit`, `Delete`, `Duplicate`, `Share`

### Suche & Recall

- `Ctrl+Shift+P` → `Memory: Recall` → Keyword eingeben
- `Ctrl+Shift+P` → `Memory: List All` → alle Memories anzeigen

### Link Validation

- `Ctrl+Shift+P` → `Memory: Validate Links`
- Findet gebrochene `[[links]]` zwischen Memories

## Konfiguration

| Setting | Default | Beschreibung |
|---------|---------|-------------|
| `memoryExplorer.enabled` | `true` | Enable/Disable Sidebar |
| `memoryExplorer.sortBy` | `"frecency"` | Sort: frecency, name, date, type |
| `memoryExplorer.showMetadata` | `true` | Zeige Metadata in Explorer |
| `memoryExplorer.autoExpand` | `false` | Auto-expand Tier-Folders |

## Struktur

```
vscode-memory-explorer/
├── package.json          # Extension Manifest
├── tsconfig.json         # TypeScript Config
├── webpack.config.js     # Build Config
├── src/
│   ├── extension.ts      # Main Extension Entry
│   ├── memoryProvider.ts # Store Abstraction
│   ├── treeView.ts       # Tree Data Provider
│   ├── treeItem.ts       # Tree Item Renderer
│   └── inlineEdit.ts     # Inline Edit Logic
├── resources/            # Icons
└── out/                  # Compiled Output
```

## Entwicklung

```bash
# Install dependencies
npm install

# Watch mode (automatisches Rebuild)
npm run watch

# Production build
npm run compile

# Lint
npm run lint

# Test
npm run test
```

## Kompatibilität

- VS Code 1.84+
- MCP Context Toolkit v1.0+
- `.talos/memory/` oder `.context/memory/` Store

## Lizenz

MIT — gleiche Lizenz wie MCP Context Toolkit

## Credits

- Othmar Atzmüller — [mcp-context-toolkit](https://github.com/othmaratzmueller-bit/mcp-context-toolkit)
