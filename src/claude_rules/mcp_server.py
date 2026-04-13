from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from claude_rules.engine import RulesEngine


def _discover_rules_dir() -> Path:
    env = os.environ.get("CLAUDE_RULES_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        rules = candidate / ".claude" / "rules"
        if rules.is_dir():
            return rules

    raise FileNotFoundError(
        "No rules directory found. Set CLAUDE_RULES_DIR or create .claude/rules/ "
        "in the current working tree."
    )


def _rule_summary(rule: Any) -> dict:
    return {
        "key": rule.key,
        "short_id": rule.short_id,
        "title": rule.title,
        "type": rule.type,
        "scope": rule.scope,
        "priority": rule.priority,
        "modules": rule.modules,
        "summary": rule.summary,
        "source_path": rule.source_path,
    }


def _rule_full(rule: Any) -> dict:
    return rule.model_dump(mode="json")


def build_server(engine: RulesEngine):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("claude-rules")

    @mcp.tool()
    def query_rules_for_file(file_path: str) -> str:
        """Return all rules whose applies_to globs match the given file path.

        Use this as the first action when opening a file in a session —
        it filters ~60 repo rules down to the ~5-10 relevant for this file.
        """
        matches = engine.query_for_file(file_path)
        return json.dumps([_rule_summary(r) for r in matches], indent=2)

    @mcp.tool()
    def get_rule(key: str) -> str:
        """Fetch the full content of a single rule by key."""
        rule = engine.get_rule(key)
        if rule is None:
            return json.dumps({"error": f"rule not found: {key}"})
        return json.dumps(_rule_full(rule), indent=2, default=str)

    @mcp.tool()
    def list_rule_keys(type: str | None = None, scope: str | None = None) -> str:
        """List all loaded rule keys, optionally filtered by type and/or scope."""
        keys = engine.list_keys(type=type, scope=scope)  # type: ignore[arg-type]
        return json.dumps(keys, indent=2)

    return mcp


def main() -> None:
    try:
        rules_dir = _discover_rules_dir()
    except FileNotFoundError as e:
        print(f"[claude-rules] {e}", file=sys.stderr)
        sys.exit(2)

    engine = RulesEngine.from_directory(rules_dir)
    print(
        f"[claude-rules] loaded {len(engine.rules)} rules from {rules_dir}",
        file=sys.stderr,
    )

    server = build_server(engine)
    server.run()


if __name__ == "__main__":
    main()
