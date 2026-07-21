// Context Toolkit — VS Code / code-server extension for the mcp-context-toolkit.
//
// Surfaces the engine's rules + memory INSIDE the editor: a sidebar of the
// loaded rules (grouped, priority-colored, searchable, with the skipped/
// invalid ones flagged), a one-click validate, "which rules apply to the file
// I'm in", and the full Context Studio (rules + memory browser) in a webview.
//
// It owns NO domain logic: every fact comes from the engine's own CLI
// (`context-toolkit-query`), so there is a single source of truth and no
// re-implemented YAML parsing (S8/W20 reuse-first). Plain CommonJS, zero npm
// dependencies, so it bakes into a container image unpacked with no build step
// (built inside/dogfooded by TALOS, but generic: no product-specific path is
// hardcoded here, see discoverRunner).

const vscode = require("vscode");
const cp = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");

// ---------------------------------------------------------------------------
// Runner discovery — how to invoke the engine CLI in this environment.
// ---------------------------------------------------------------------------
// A product embedding this extension in a container points CONTEXT_ENGINE_PYTHON
// at its own pinned interpreter (with the engine importable via that interpreter's
// PYTHONPATH — set by the SAME container config, not by this extension). Same
// override convention as the sibling vscode-memory-explorer's CONTEXT_* env vars
// (CONTEXT_STORE_CONVENTIONS/CONTEXT_USER_MEMORY_DIR) — no product-specific path
// is hardcoded here; a fork would defeat that. On a plain dev box neither is set,
// so we fall back to the repo's own engine venv next to the workspace, and
// finally to a `context-toolkit-query` on PATH.
function discoverRunner() {
  const env = { ...process.env };
  env.CONTEXT_STORE_CONVENTIONS = env.CONTEXT_STORE_CONVENTIONS || ".context,.claude";
  env.CONTEXT_USER_MEMORY_DIR =
    env.CONTEXT_USER_MEMORY_DIR || path.join(os.homedir(), ".context", "memory");

  const pinnedPy = env.CONTEXT_ENGINE_PYTHON;
  if (pinnedPy && fs.existsSync(pinnedPy)) {
    return { cmd: pinnedPy, base: ["-m", "mcp_context_toolkit.cli"], env };
  }
  // Dev fallback: a repo checkout with an engine venv next to the workspace.
  const ws = firstWorkspaceDir();
  if (ws) {
    for (const rel of [".claude/engine", "engine"]) {
      const engineRoot = path.join(ws, rel);
      const devPy = path.join(engineRoot, ".venv", "bin", "python");
      const devSrc = path.join(engineRoot, "src");
      if (fs.existsSync(devPy) && fs.existsSync(devSrc)) {
        env.PYTHONPATH = [devSrc, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
        return { cmd: devPy, base: ["-m", "mcp_context_toolkit.cli"], env, viewerDir: path.join(devSrc, "mcp_context_toolkit", "viewer") };
      }
    }
  }
  // Last resort: a globally installed console script.
  return { cmd: "context-toolkit-query", base: [], env, viewerDir: null };
}

function firstWorkspaceDir() {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length ? folders[0].uri.fsPath : undefined;
}

// Run the CLI, resolve with stdout (rejects on non-zero exit, surfacing stderr).
function runCli(runner, args, { allowNonZero = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = cp.spawn(runner.cmd, [...runner.base, ...args], {
      env: runner.env,
      cwd: firstWorkspaceDir() || os.homedir(),
    });
    let out = "";
    let err = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (err += d));
    child.on("error", (e) => reject(e));
    child.on("close", (code) => {
      if (code === 0 || allowNonZero) resolve({ stdout: out, stderr: err, code });
      else reject(new Error(`context-toolkit-query exited ${code}: ${err.trim() || out.trim()}`));
    });
  });
}

// ---------------------------------------------------------------------------
// Data layer — one studio export feeds both the tree and the webview.
// ---------------------------------------------------------------------------
class Store {
  constructor(runner, exportDir) {
    this.runner = runner;
    this.exportDir = exportDir;
    this.rules = null; // parsed rules.json
    this.memory = null; // parsed memory.json (may stay null — no store)
    this.pending = null; // raw pending.md (the /dream ledger, may stay null — none open)
    this.diff = null; // parsed diff.json (canonical _PENDING_DIFF.json, may stay null — none open)
    this.error = null;
  }

  async refresh() {
    this.error = null;
    try {
      fs.mkdirSync(this.exportDir, { recursive: true });
      await runCli(this.runner, ["--export-studio", this.exportDir]);
      this.rules = readJsonOrNull(path.join(this.exportDir, "rules.json"));
      this.memory = readJsonOrNull(path.join(this.exportDir, "memory.json"));
      this.pending = readTextOrNull(path.join(this.exportDir, "pending.md"));
      this.diff = readJsonOrNull(path.join(this.exportDir, "diff.json"));
    } catch (e) {
      this.error = e.message || String(e);
      this.rules = null;
      this.memory = null;
      this.pending = null;
      this.diff = null;
    }
  }

  ruleList() {
    return this.rules && Array.isArray(this.rules.rules) ? this.rules.rules : [];
  }

  skipped() {
    return this.rules && Array.isArray(this.rules.skipped) ? this.rules.skipped : [];
  }
}

function readJsonOrNull(p) {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (_) {
    return null;
  }
}

function readTextOrNull(p) {
  return fs.existsSync(p) ? fs.readFileSync(p, "utf8") : null;
}

// ---------------------------------------------------------------------------
// Sidebar — a themed webview view (not a plain TreeView): grouped by type,
// priority-colored, searchable. Replaces the earlier bare TreeView, which had
// no way to show more than a label+description per row (no color, no filter).
// ---------------------------------------------------------------------------
const TYPE_LABEL = {
  security: "Security",
  code_quality: "Code Quality",
  frontend: "Frontend",
  workflow: "Workflow",
  infrastructure: "Infrastructure",
};
const PRIORITY_ORDER = { non_negotiable: 0, mandatory: 1, recommended: 2 };
const PRIORITY_DOT = {
  non_negotiable: "var(--vscode-charts-red)",
  mandatory: "var(--vscode-charts-yellow)",
  recommended: "var(--vscode-charts-blue)",
};

class RulesViewProvider {
  constructor(store) {
    this.store = store;
    this._view = null;
  }

  resolveWebviewView(webviewView) {
    this._view = webviewView;
    webviewView.webview.options = { enableScripts: true };
    webviewView.webview.onDidReceiveMessage((msg) => {
      if (!msg) return;
      if (msg.command === "openRule" && msg.path) openRule(msg.path);
      else if (msg.command === "openStudio") vscode.commands.executeCommand("contextToolkit.openStudio");
    });
    this.refresh();
  }

  refresh() {
    if (this._view) this._view.webview.html = rulesViewHtml(this._view.webview, this.store);
  }
}

function ruleRowHtml(r) {
  const dot = PRIORITY_DOT[r.priority] || "var(--vscode-descriptionForeground)";
  const id = r.short_id ? `[${escapeHtml(r.short_id)}] ` : "";
  const tier = r.tier && r.tier !== "project" ? ` · ${escapeHtml(r.tier)}` : "";
  const key = r.key ? ` data-key="${escapeHtml(r.key)}"` : "";
  const summary = escapeHtml((r.summary || "").trim());
  return `<div class="row${r.key ? " clickable" : ""}"${key} title="${summary}">
    <span class="dot" style="background:${dot}"></span>
    <span class="label">${id}${escapeHtml(r.title || r.key)}</span>
    <span class="meta">${escapeHtml(r.priority || "")}${tier}</span>
  </div>`;
}

function skippedRowHtml(msg) {
  // msg looks like "[project] /path/to/file.yaml: <reason>" — pull the path
  // between the tier tag and the first colon after it for the display label.
  const label = msg.replace(/^\[[^\]]*\]\s*/, "").split(":")[0].trim();
  return `<div class="row skip" title="${escapeHtml(msg)}">${escapeHtml(path.basename(label) || label)}</div>`;
}

function rulesViewHtml(webview, store) {
  const nonce = makeNonce();
  const csp = `default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';`;
  const css = `
    body { margin:0; font:12px var(--vscode-font-family,sans-serif); color:var(--vscode-foreground); }
    .toolbar { display:flex; align-items:center; gap:8px; padding:6px 8px; position:sticky; top:0;
               background:var(--vscode-sideBar-background); border-bottom:1px solid var(--vscode-widget-border,transparent); }
    #search { flex:1; background:var(--vscode-input-background); color:var(--vscode-input-foreground);
              border:1px solid var(--vscode-input-border,transparent); border-radius:3px; padding:3px 6px; font:inherit; }
    .total { color:var(--vscode-descriptionForeground); white-space:nowrap; }
    details.group { border-bottom:1px solid var(--vscode-widget-border,transparent); }
    summary { cursor:pointer; padding:5px 8px; font-weight:600; user-select:none; }
    summary .count { color:var(--vscode-descriptionForeground); font-weight:400; margin-left:4px; }
    .row { display:flex; align-items:center; gap:6px; padding:4px 8px 4px 20px; }
    .row.clickable { cursor:pointer; }
    .row.clickable:hover { background:var(--vscode-list-hoverBackground); }
    .dot { width:8px; height:8px; border-radius:50%; flex:none; }
    .label { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .meta { color:var(--vscode-descriptionForeground); font-size:11px; white-space:nowrap; }
    .row.skip { color:var(--vscode-errorForeground); }
    .banner.error { margin:8px; padding:8px; background:var(--vscode-inputValidation-errorBackground);
                    border:1px solid var(--vscode-inputValidation-errorBorder,transparent); }
    .empty { padding:12px; color:var(--vscode-descriptionForeground); }
    .detail { padding:8px 8px 10px 20px; background:var(--vscode-sideBarSectionHeader-background,transparent);
              border-bottom:1px solid var(--vscode-widget-border,transparent); }
    .badges { display:flex; flex-wrap:wrap; gap:4px; margin-bottom:6px; }
    .badge { padding:1px 7px; border-radius:9px; font-size:10px; font-weight:600;
             color:var(--vscode-editor-background); }
    .badge.non_negotiable { background:var(--vscode-charts-red); }
    .badge.mandatory { background:var(--vscode-charts-yellow); }
    .badge.recommended { background:var(--vscode-charts-blue); }
    .badge.tier, .badge.tag { background:var(--vscode-badge-background); color:var(--vscode-badge-foreground); font-weight:400; }
    .summary { margin:0 0 8px; white-space:pre-wrap; line-height:1.4; }
    .detail-heading { font-weight:600; font-size:11px; text-transform:uppercase;
                       color:var(--vscode-descriptionForeground); margin-bottom:3px; }
    .globs { margin:0 0 8px; padding-left:16px; }
    .globs code { font-family:var(--vscode-editor-font-family,monospace); font-size:11px; }
    .open-source { font:inherit; padding:3px 10px; border:1px solid var(--vscode-button-border,transparent);
                    border-radius:3px; background:var(--vscode-button-secondaryBackground);
                    color:var(--vscode-button-secondaryForeground); cursor:pointer; }
    .open-source:hover { background:var(--vscode-button-secondaryHoverBackground); }
  `;

  if (store.error) {
    return `<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="${csp}">
      <style>${css}</style></head><body>
      <div class="banner error">Engine nicht erreichbar<br><small>${escapeHtml(store.error)}</small></div>
      </body></html>`;
  }

  const byType = new Map();
  for (const r of store.ruleList()) {
    const t = r.type || "other";
    if (!byType.has(t)) byType.set(t, []);
    byType.get(t).push(r);
  }
  const groupsHtml = [...byType.keys()]
    .sort()
    .map((t) => {
      const rules = byType
        .get(t)
        .slice()
        .sort(
          (a, b) =>
            (PRIORITY_ORDER[a.priority] ?? 9) - (PRIORITY_ORDER[b.priority] ?? 9) ||
            String(a.short_id || a.key).localeCompare(String(b.short_id || b.key))
        );
      return `<details class="group" open>
        <summary>${escapeHtml(TYPE_LABEL[t] || t)}<span class="count">${rules.length}</span></summary>
        <div class="rows">${rules.map(ruleRowHtml).join("")}</div>
      </details>`;
    })
    .join("");

  const skipped = store.skipped();
  const skippedHtml = skipped.length
    ? `<details class="group">
        <summary>⚠ Übersprungen<span class="count">${skipped.length}</span></summary>
        <div class="rows">${skipped.map(skippedRowHtml).join("")}</div>
      </details>`
    : "";

  const body = groupsHtml || skippedHtml
    ? `${skippedHtml}${groupsHtml}`
    : `<div class="empty">Keine Regeln geladen.</div>`;

  return `<!DOCTYPE html>
<html>
<head><meta http-equiv="Content-Security-Policy" content="${csp}"><style>${css}</style></head>
<body>
  <div class="toolbar">
    <input id="search" type="text" placeholder="Filter…">
    <span class="total">${store.ruleList().length} Regeln</span>
  </div>
  ${body}
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    // Same data already rendered into the rows — re-used here for a structured
    // detail view on click, so expanding a rule needs no round-trip to the host.
    const RULES = ${jsonForScript(Object.fromEntries(store.ruleList().map((r) => [r.key, r])))};

    function buildDetail(r) {
      const wrap = document.createElement("div");
      wrap.className = "detail";

      const badges = document.createElement("div");
      badges.className = "badges";
      const prio = document.createElement("span");
      prio.className = "badge " + (r.priority || "");
      prio.textContent = r.priority || "";
      badges.appendChild(prio);
      if (r.tier) {
        const t = document.createElement("span");
        t.className = "badge tier";
        t.textContent = r.tier;
        badges.appendChild(t);
      }
      (r.tags || []).forEach((tag) => {
        const t = document.createElement("span");
        t.className = "badge tag";
        t.textContent = tag;
        badges.appendChild(t);
      });
      wrap.appendChild(badges);

      if (r.summary) {
        const p = document.createElement("p");
        p.className = "summary";
        p.textContent = r.summary;
        wrap.appendChild(p);
      }

      if (r.files && r.files.length) {
        const h = document.createElement("div");
        h.className = "detail-heading";
        h.textContent = "Gilt für";
        wrap.appendChild(h);
        const ul = document.createElement("ul");
        ul.className = "globs";
        r.files.forEach((f) => {
          const li = document.createElement("li");
          const code = document.createElement("code");
          code.textContent = f;
          li.appendChild(code);
          ul.appendChild(li);
        });
        wrap.appendChild(ul);
      }

      if (r.source_path) {
        const btn = document.createElement("button");
        btn.className = "open-source";
        btn.textContent = "Quelle öffnen";
        btn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          vscode.postMessage({ command: "openRule", path: r.source_path });
        });
        wrap.appendChild(btn);
      }
      return wrap;
    }

    let openRow = null;
    let openPanel = null;
    function closeDetail() {
      if (openPanel) openPanel.remove();
      openRow = null;
      openPanel = null;
    }

    document.getElementById("search").addEventListener("input", (e) => {
      closeDetail();
      const q = e.target.value.toLowerCase();
      document.querySelectorAll(".row").forEach((row) => {
        row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });

    document.querySelectorAll(".row.clickable").forEach((row) => {
      row.addEventListener("click", () => {
        const wasOpenRow = openRow;
        closeDetail();
        if (wasOpenRow === row) return; // click on the already-open row: just collapse
        const r = RULES[row.dataset.key];
        if (!r) return;
        openRow = row;
        openPanel = buildDetail(r);
        row.insertAdjacentElement("afterend", openPanel);
      });
    });
  </script>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// Studio webview — embed the engine's own bundled viewer, data injected.
// ---------------------------------------------------------------------------
let STUDIO_PANEL = null;

function openStudio(context, store) {
  if (STUDIO_PANEL) {
    STUDIO_PANEL.reveal(vscode.ViewColumn.Active);
    STUDIO_PANEL.webview.html = studioHtml(STUDIO_PANEL.webview, store);
    return;
  }
  STUDIO_PANEL = vscode.window.createWebviewPanel(
    "contextToolkitStudio",
    "Context Studio",
    vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true, localResourceRoots: [vscode.Uri.file(store.exportDir)] }
  );
  STUDIO_PANEL.iconPath = new vscode.ThemeIcon("law");
  STUDIO_PANEL.webview.html = studioHtml(STUDIO_PANEL.webview, store);
  STUDIO_PANEL.onDidDispose(() => (STUDIO_PANEL = null), null, context.subscriptions);
}

function studioHtml(webview, store) {
  const indexPath = path.join(store.exportDir, "index.html");
  if (!fs.existsSync(indexPath)) {
    return `<html><body style="font-family:sans-serif;padding:2rem;color:#e3e8f0;background:#0f1218">
      <h3>Studio nicht verfügbar</h3>
      <p>Der Viewer wurde nicht exportiert. Ist die Engine erreichbar?</p>
      <pre>${escapeHtml(store.error || "unbekannt")}</pre></body></html>`;
  }
  let html = fs.readFileSync(indexPath, "utf8");
  const nonce = makeNonce();

  // Rewrite the external cytoscape script to a webview-safe URI.
  const cytoPath = path.join(store.exportDir, "cytoscape.min.js");
  if (fs.existsSync(cytoPath)) {
    const cytoUri = webview.asWebviewUri(vscode.Uri.file(cytoPath));
    html = html.replace(
      /<script src="cytoscape\.min\.js"><\/script>/,
      `<script nonce="${nonce}" src="${cytoUri}"></script>`
    );
  }

  // Nonce every bare inline <script> block — the viewer's index.html has two:
  // the early theme-toggle script in <head> and the main app script after the
  // cytoscape include. A non-global replace only caught the first (the theme
  // toggle), leaving the main script un-nonced — the CSP below silently blocked
  // it, so the shell rendered but nothing was interactive (no tab switching, no
  // rules/memory data, no graph).
  html = html.replaceAll("<script>", `<script nonce="${nonce}">`);

  // Inject the data the viewer reads from window.CONTEXT_DATA (short-circuits its
  // fetch of ./rules.json|./memory.json, which the webview CSP would block anyway).
  // `pending`/`diff` are the viewer's documented host-injectable slots (frontend/
  // README.md "Host hooks" + "Three ways the viewer finds data") — both are already
  // resolved from the canonical _DREAM_PENDING.md / _PENDING_DIFF.json at export
  // time, so opening Studio never requires a manual drag-and-drop onto Review.
  const data = {
    rules: store.rules || undefined,
    memory: store.memory || undefined,
    pending: store.pending || undefined,
    diff: store.diff || undefined,
  };
  const dataScript = `<script nonce="${nonce}">window.CONTEXT_DATA=${jsonForScript(data)};</script>`;
  html = html.replace(/<\/head>/, `${dataScript}\n</head>`);

  // Content-Security-Policy: no network, scripts only via our nonce, styles
  // inline-allowed (the viewer ships a large inline <style>; styles cannot
  // execute). Images incl. data: for the frecency heat swatches.
  const csp =
    `default-src 'none'; ` +
    `img-src ${webview.cspSource} data:; ` +
    `style-src ${webview.cspSource} 'unsafe-inline'; ` +
    `script-src 'nonce-${nonce}' ${webview.cspSource}; ` +
    `font-src ${webview.cspSource};`;
  const cspTag = `<meta http-equiv="Content-Security-Policy" content="${csp}">`;
  html = html.replace(/<head>/, `<head>\n${cspTag}`);

  return html;
}

// JSON safe to embed inside a <script> block: escape the tag-opening bracket so
// a string value can never terminate the script element early.
function jsonForScript(obj) {
  // Replace every "<" with its < escape so a string value inside the JSON
  // can never spell "</script>" and terminate the embedding <script> element.
  return JSON.stringify(obj).replaceAll("<", "\\u003c");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function makeNonce() {
  // CSPRNG, not Math.random — the CSP nonce is a security token; a predictable
  // nonce would (in principle) let injected markup guess it. 24 bytes → 32 base64 chars.
  return crypto.randomBytes(24).toString("base64");
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------
async function cmdValidate(runner, output) {
  try {
    const res = await runCli(runner, ["--validate"], { allowNonZero: true });
    let parsed;
    try {
      parsed = JSON.parse(res.stdout);
    } catch (_) {
      vscode.window.showErrorMessage("Validate: unerwartete Ausgabe (kein JSON).");
      output.appendLine(res.stdout);
      output.appendLine(res.stderr);
      output.show(true);
      return;
    }
    const errs = parsed.errors || [];
    const warns = parsed.warnings || [];
    if (parsed.ok && !warns.length) {
      vscode.window.showInformationMessage(`Regeln OK — ${parsed.rule_count} geladen, keine Fehler.`);
      return;
    }
    output.clear();
    output.appendLine(`Regeln: ${parsed.rule_count} · Fehler: ${errs.length} · Warnungen: ${warns.length}`);
    for (const e of errs) output.appendLine(`  FEHLER  ${e}`);
    for (const w of warns) output.appendLine(`  WARN    ${w}`);
    output.show(true);
    if (errs.length) vscode.window.showErrorMessage(`${errs.length} Regel-Fehler — Details im Ausgabe-Panel.`);
    else vscode.window.showWarningMessage(`${warns.length} Regel-Warnungen — Details im Ausgabe-Panel.`);
  } catch (e) {
    vscode.window.showErrorMessage(`Validate fehlgeschlagen: ${e.message}`);
  }
}

async function cmdRulesForCurrentFile(runner) {
  const ed = vscode.window.activeTextEditor;
  if (!ed) {
    vscode.window.showInformationMessage("Keine Datei offen.");
    return;
  }
  const ws = firstWorkspaceDir();
  const abs = ed.document.uri.fsPath;
  const rel = ws && abs.startsWith(ws) ? path.relative(ws, abs) : abs;
  try {
    const res = await runCli(runner, [rel, "--format", "json"]);
    const parsed = JSON.parse(res.stdout);
    const rules = parsed.rules || [];
    if (!rules.length) {
      vscode.window.showInformationMessage(`Keine Regeln greifen auf ${rel}.`);
      return;
    }
    const picks = rules.map((r) => ({
      label: `${r.short_id ? `[${r.short_id}] ` : ""}${r.title || r.key}`,
      description: r.priority,
      detail: (r.summary || "").trim(),
    }));
    vscode.window.showQuickPick(picks, {
      title: `${rules.length} Regel(n) für ${rel}`,
      matchOnDetail: true,
    });
  } catch (e) {
    vscode.window.showErrorMessage(`Regeln-für-Datei fehlgeschlagen: ${e.message}`);
  }
}

async function openRule(sourcePath) {
  try {
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(sourcePath));
    await vscode.window.showTextDocument(doc, { preview: true });
  } catch (e) {
    vscode.window.showErrorMessage(`Kann Regel-Datei nicht öffnen: ${e.message}`);
  }
}

// ---------------------------------------------------------------------------
// Activation
// ---------------------------------------------------------------------------
function activate(context) {
  const runner = discoverRunner();
  // The studio export (rules.json/memory.json/viewer) is a REGENERATED cache —
  // written to an ephemeral OS temp dir, NOT context.globalStorageUri. The latter
  // lives on the per-user coder-vscode volume (backup-scoped, flagged unencrypted),
  // and persisting a derived snapshot of the project rules/memory there buys nothing
  // but a new at-rest copy. /tmp is per-container (per-user) and cleared on teardown.
  const exportDir = path.join(os.tmpdir(), "context-toolkit-studio");
  const store = new Store(runner, exportDir);
  const output = vscode.window.createOutputChannel("Context Toolkit");
  const rulesView = new RulesViewProvider(store);

  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.command = "contextToolkit.openStudio";
  status.text = "$(law) Regeln …";
  status.tooltip = "Context Toolkit — Studio öffnen";
  status.show();

  async function reload() {
    await store.refresh();
    rulesView.refresh();
    if (store.error) {
      status.text = "$(law) Regeln —";
      status.tooltip = `Context Toolkit: Engine nicht erreichbar\n${store.error}`;
    } else {
      const n = store.ruleList().length;
      const skipped = store.skipped().length;
      status.text = skipped ? `$(law) ${n} Regeln · $(warning) ${skipped}` : `$(law) ${n} Regeln`;
      status.tooltip = skipped
        ? `Context Toolkit: ${n} Regeln geladen, ${skipped} übersprungen (Schema-Fehler) — Studio öffnen`
        : `Context Toolkit: ${n} Regeln geladen — Studio öffnen`;
    }
  }

  context.subscriptions.push(
    output,
    status,
    vscode.window.registerWebviewViewProvider("contextRules", rulesView),
    vscode.commands.registerCommand("contextToolkit.refresh", reload),
    vscode.commands.registerCommand("contextToolkit.openStudio", async () => {
      if (!store.rules && !store.error) await reload();
      openStudio(context, store);
    }),
    vscode.commands.registerCommand("contextToolkit.validate", () => cmdValidate(runner, output)),
    vscode.commands.registerCommand("contextToolkit.rulesForCurrentFile", () => cmdRulesForCurrentFile(runner)),
    vscode.commands.registerCommand("contextToolkit.openRule", (p) => openRule(p)),
    vscode.commands.registerCommand("contextToolkit.showError", (msg) => {
      output.clear();
      output.appendLine(msg || "unbekannter Fehler");
      output.show(true);
    })
  );

  // Auto-refresh on rule/memory changes — no more manual refresh-click needed
  // to see e.g. a fresh /dream pending ledger or an edited rule show up.
  const ws = firstWorkspaceDir();
  if (ws) {
    const watcher = vscode.workspace.createFileSystemWatcher(
      new vscode.RelativePattern(ws, "{.claude/rules/**,.claude/memory/**}")
    );
    watcher.onDidChange(reload);
    watcher.onDidCreate(reload);
    watcher.onDidDelete(reload);
    context.subscriptions.push(watcher);
  }

  reload();
}

function deactivate() {}

module.exports = { activate, deactivate };
