# Architecture

How a query flows from an MCP client (or hook) through the engine to the
markdown stores on disk.

```mermaid
flowchart LR
    subgraph client["MCP client / agent session"]
        HOOKS["Hooks<br/>(PreToolUse: file touch,<br/>UserPromptSubmit: prompt)"]
        TOOLS["MCP tool calls<br/>(query_rules_for_file, recall,<br/>get_rule, get_memory, ...)"]
        CLI["CLI<br/>(context-toolkit-query,<br/>--format bundle)"]
    end

    subgraph server["mcp_server.py"]
        RELOADER["_Reloader<br/>(mtime check per call,<br/>reload on real change)"]
    end

    subgraph engine["engine.py / core.py"]
        RULES["Rules engine<br/>glob match by file path,<br/>priority sort, tiered<br/>(project > shared)"]
        DECISIONS["Decisions (ADRs)<br/>glob match, status cut,<br/>DECISION_TOP_K newest"]
        MEMORY["memory.py<br/>recall: relevance x frecency<br/>+ backlink boost,<br/>tiered (project > user > core)"]
        USAGE["usage.py<br/>hit tracking -> frecency"]
    end

    subgraph store["markdown + YAML stores (git-backed)"]
        RDIR["rules/**/*.yaml"]
        DDIR["decisions/*.yaml"]
        MDIR["memory/**/*.md<br/>+ MEMORY.md hot index"]
    end

    HOOKS --> server
    TOOLS --> server
    CLI --> engine
    server --> RELOADER --> engine
    RULES --> RDIR
    DECISIONS --> DDIR
    MEMORY --> MDIR
    MEMORY --> USAGE

    subgraph offline["offline / maintenance"]
        INDEXER["indexer.py<br/>regenerates MEMORY.md"]
        BUNDLER["bundler.py<br/>consolidation bundles"]
    end
    INDEXER --> MDIR
    BUNDLER --> MDIR
```

## Design points

- **Stateless per call.** Every query reads from the in-memory model; the
  `_Reloader` re-loads from disk only when the store's max mtime actually
  changed — no file-watcher daemon, safe under concurrent sessions.
- **Tiers, not merges.** Multiple roots (project / shared / user / core) stay
  separate; on a key collision the more specific tier wins. Rules fall back to
  the shared "discipline floor" only when a file matches zero project rules.
- **The store is the API.** Plain markdown/YAML in git — editable by humans,
  agents, and consolidation passes alike. The engine never writes to the store;
  writers are the operator and offline tools (`indexer.py`, `bundler.py`).
- **Frecency, not recency.** `usage.py` records hits per memory; recall ranks
  by keyword relevance weighted with frequency + recency, so long-lived,
  often-used knowledge stays hot without manual pinning. On top, the MCP
  `recall` tool adds a log-dampened **backlink boost** (`log1p(inbound) * 0.1`)
  from the `[[link]]` graph, so structurally central memories rise without
  explicit usage.
