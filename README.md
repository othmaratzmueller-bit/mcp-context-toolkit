# claude-rules

Granular rule loading for Claude Code sessions.

Rules live in `.claude/rules/**/*.yaml` inside each repo. A stdio MCP server
exposes three tools that let a session query only the rules relevant for the
file it is currently working on, instead of loading a monolithic `CLAUDE.md`.

## Design in one sentence

Each rule declares `applies_to.files` globs; the engine returns only rules whose
globs match a given file path, sorted by priority — so a Frontend session never
sees Backend security rules in its context.

## Status

v0.1 — core engine + MCP server + example rules. No storage, no auto-reload,
no compliance bridge yet. Runs in-memory, zero native deps beyond Python stdlib
plus `pydantic`, `pyyaml`, `mcp`.

## Install

```bash
cd claude-rules
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run tests

```bash
pytest
```

## Register with Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "claude-rules": {
      "command": "claude-rules-mcp",
      "env": {
        "CLAUDE_RULES_DIR": "/absolute/path/to/your/repo/.claude/rules"
      }
    }
  }
}
```

If `CLAUDE_RULES_DIR` is not set, the server walks up from the CWD looking for
`.claude/rules/`.

## Rule schema

See `examples/no_hardcoded_secrets.yaml` for a full example;
`examples/function_length_limit.yaml` and `examples/no_inline_event_handlers.yaml`
show other rule types and priorities. Required fields:

- `key` (snake_case, unique)
- `title`
- `type` — one of `security | workflow | code_quality | frontend | architecture | infrastructure | module`
- `scope` — one of `backend | frontend | database | infrastructure | docs | all`
- `priority` — one of `non_negotiable | mandatory | recommended`
- `modules` — list of module names, or `[all]`
- `applies_to.files` — list of globs (supports `**` recursive)
- `summary` — short one-liner shown in query results
- `content` — full markdown body fetched via `get_rule`
- `created` — ISO date

## Tools exposed

| Tool | Purpose |
| --- | --- |
| `query_rules_for_file(file_path)` | Return rule summaries for all rules whose globs match the file |
| `get_rule(key)` | Return the full content of one rule |
| `list_rule_keys(type?, scope?)` | Enumerate rule keys, optionally filtered |

## Layout

```
claude-rules/
├── src/claude_rules/
│   ├── models.py        Pydantic Rule schema
│   ├── engine.py        Load + query
│   ├── mcp_server.py    stdio MCP wrapper
│   └── cli.py           claude-rules-query CLI
├── examples/
│   ├── no_hardcoded_secrets.yaml     (security, non_negotiable)
│   ├── function_length_limit.yaml    (code_quality, mandatory)
│   └── no_inline_event_handlers.yaml (frontend, recommended)
├── tests/
│   └── test_engine.py
├── LICENSE
└── pyproject.toml
```

## License

MIT — see `LICENSE`. The engine is deliberately generic and ships only with
example rules; project-specific rule sets live in each consuming repo's
`.claude/rules/` and are not part of this package.
