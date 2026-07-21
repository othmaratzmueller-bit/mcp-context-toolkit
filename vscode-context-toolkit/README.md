# Context Toolkit — VS Code / code-server Extension

Macht die **mcp-context-toolkit** Regeln und das Memory direkt im Editor
sichtbar und pflegbar — ohne eigenes Web-Frontend. Generisch: kein
produkt-spezifischer Pfad ist hartkodiert (siehe „Konfiguration"). Gebaut
innerhalb/dogfooded von TALOS, in dessen Coder-Container-Image eingebacken.

## Was es kann

- **Regel-Baum** (Activity-Bar → Context Toolkit): alle geladenen Regeln nach
  Typ gruppiert (Security / Code Quality / Frontend / Workflow / Infrastructure),
  mit Priorität und Tier je Regel. Klick öffnet die YAML-Quelle.
- **Übersprungen-Knoten**: schema-invalide Regel-Dateien werden oben rot geflaggt
  (nicht mehr still geschluckt). Klick öffnet die kaputte Datei.
- **Studio öffnen**: bettet den vollen Context-Studio-Viewer (Regeln + Memory,
  Frecency-Heat, Abhängigkeits-Graph) als Webview ein.
- **Regeln validieren**: `--validate` auf Knopfdruck, Fehler/Warnungen ins
  Ausgabe-Panel.
- **Regeln für die aktuelle Datei**: zeigt per Quick-Pick, welche Regeln auf die
  gerade offene Datei greifen (dieselbe Glob-Logik wie der Pre-Edit-Hook).
- **Statusleiste**: `N Regeln · ⚠ M übersprungen`, Klick öffnet das Studio.

## Wie es an die Daten kommt

Das Plugin besitzt **keine** eigene Regel-Logik. Jede Zahl, jede Regel, jede
Validierung kommt aus der Engine-CLI `context-toolkit-query` — eine einzige
Wahrheitsquelle, kein nachgebautes YAML-Parsing (S8/W20 reuse-first). Ein
`--export-studio`-Aufruf speist Baum **und** Webview.

Discovery der CLI (in dieser Reihenfolge):

1. **`CONTEXT_ENGINE_PYTHON`**: falls gesetzt und der Pfad existiert, wird
   dieser Interpreter mit `-m mcp_context_toolkit.cli` aufgerufen (die Engine
   muss über dessen `PYTHONPATH` importierbar sein — beides setzt das
   einbettende Produkt in seiner eigenen Container-Config, z.B. Docker `ENV`,
   NIE hier im Plugin-Code).
2. **Dev-Checkout**: das Engine-venv neben dem Workspace
   (`.claude/engine/.venv/bin/python`).
3. **Fallback**: ein global installiertes `context-toolkit-query` auf `PATH`.

## Konfiguration (Host-Wiring)

Dieselbe Override-Konvention wie beim Memory-Explorer und der Engine selbst —
ein einbettendes Produkt setzt diese Env-Variablen container-/session-weit
(z.B. TALOS via Docker `ENV` im Coder-Image), nie geforkt im Plugin-Code:

| Env-Var | Generischer Default | Zweck |
|---|---|---|
| `CONTEXT_ENGINE_PYTHON` | *(unset → Dev-Fallback)* | Pfad zum gepinnten Interpreter mit der Engine im `PYTHONPATH` |
| `CONTEXT_STORE_CONVENTIONS` | `.context,.claude` | Kandidaten-Ordnernamen fürs Projekt-Tier (z.B. `.talos,.context,.claude`) |
| `CONTEXT_USER_MEMORY_DIR` | `~/.context/memory` | User-Tier (cross-project) Memory-Verzeichnis |
| `CONTEXT_SHARED_RULES_DIR` | *(unset = kein shared Tier)* | Optionale, produkt-weite „Grundregeln"-Etage |

## Sicherheit

Der Studio-Viewer läuft im Webview unter strenger CSP: `default-src 'none'`,
Skripte nur mit Per-Render-Nonce, kein Netzwerk. Die Daten werden als
`window.CONTEXT_DATA` injiziert (der Viewer liest genau diesen Pfad), `<`
wird escaped — kein `</script>`-Breakout. Der Viewer selbst rendert alle Daten
XSS-sicher über `textContent`/`createElement`.

## Kein Build-Step

Reines CommonJS, null npm-Abhängigkeiten (nur die `vscode`-API + Node-Builtins).
Wird unentpackt ins Coder-Image gebacken — kein `npm install`, kein Bundler.

MIT · by Othmar Atzmüller (SPS Technik)
