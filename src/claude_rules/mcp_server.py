from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from claude_rules.engine import RulesEngine
from claude_rules.memory import MemoryEngine
from claude_rules.usage import UsageStore


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


def _discover_memory_dir() -> Path | None:
    """Locate the PROJECT memory directory. Env CLAUDE_MEMORY_DIR wins, else
    walk up for .claude/memory. Returns None if none found — memory tools are
    then simply not registered and rules keep working untouched."""
    env = os.environ.get("CLAUDE_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        mem = candidate / ".claude" / "memory"
        if mem.is_dir():
            return mem
    return None


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


def _memory_summary(m: Any) -> dict:
    return {
        "name": m.name,
        "type": m.type,
        "tier": m.tier,
        "description": m.description,
        "source_path": m.source_path,
    }


def build_server(
    engine: RulesEngine,
    memory_engine: MemoryEngine | None = None,
    usage: UsageStore | None = None,
):
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

    @mcp.tool()
    def validate_rules() -> str:
        """Re-validate all rules from disk. Returns {ok, rule_count, errors, warnings}.

        Use this to check whether a recent YAML edit broke the rule set,
        or whether any `conflicts_with` references point to non-existent
        rules. Does NOT mutate the running engine's rule cache — it reads
        fresh from disk.
        """
        import json as _json
        from pathlib import Path as _Path

        # Re-run validation against the directory the engine was loaded from.
        # We pull rules_dir from the first rule's source_path (all rules live
        # under the same root).
        if not engine.rules:
            return _json.dumps({"ok": False, "error": "no rules loaded"})
        source = engine.rules[0].source_path
        if source is None:
            return _json.dumps({"ok": False, "error": "no source_path on rules"})
        root = _Path(source).parent
        while root.name and root.name != "rules":
            root = root.parent
        result = RulesEngine.validate_directory(root)
        return _json.dumps(result, indent=2)

    @mcp.tool()
    def query_rules(
        type: str | None = None,
        scope: str | None = None,
        priority: str | None = None,
        module: str | None = None,
    ) -> str:
        """Return rule summaries matching optional filters. Bulk fetch for agents.

        Unlike query_rules_for_file (which matches globs against a path),
        query_rules filters by metadata fields: type (security, workflow,
        code_quality, frontend, architecture, infrastructure, module),
        scope (backend, frontend, database, infrastructure, docs, all),
        priority (non_negotiable, mandatory, recommended), and module name.

        Returns sorted by priority (non_negotiable first), then by key.
        Each result is a summary dict — use get_rule(key) for the full
        content of a specific rule.

        Typical uses:
          - Security review: query_rules(type="security", priority="non_negotiable")
          - Frontend session: query_rules(scope="frontend")
          - Themis module: query_rules(module="themis")
        """
        matches = engine.query(
            type=type,  # type: ignore[arg-type]
            scope=scope,  # type: ignore[arg-type]
            module=module,
            priority=priority,  # type: ignore[arg-type]
        )
        matches.sort(
            key=lambda r: (
                {"non_negotiable": 0, "mandatory": 1, "recommended": 2}[r.priority],
                r.key,
            )
        )
        return json.dumps([_rule_summary(r) for r in matches], indent=2)

    # ---- memory tools (registered only when a memory store was found) ----
    if memory_engine is not None:

        @mcp.tool()
        def recall(query: str, limit: int = 8) -> str:
            """Recall the most relevant memories for a query, ranked across BOTH
            tiers (project + user). Keyword-based, deterministic, hot/cold-aware.

            The native auto-memory only surfaces the project tier — use this to
            pull project AND user-global knowledge on demand. Ranking blends
            keyword relevance with frecency (frequently/recently used memories
            are pulled forward). Returns summaries; follow up with
            get_memory(name) for the full body — which also marks it as used.
            """
            boosts = usage.boosts() if usage is not None else {}
            hits = memory_engine.recall(query, limit=limit, boost=boosts)
            if usage is not None:
                usage.record_recall([m.name for m in hits])
            return json.dumps(
                [_memory_summary(m) for m in hits], indent=2, ensure_ascii=False
            )

        @mcp.tool()
        def get_memory(name: str) -> str:
            """Fetch the full body + metadata of a single memory by its name.

            Counts as a strong usage hit (heavier than a recall appearance) —
            this is what makes a memory 'hot' and pulls it forward in future
            recall results and in the /dream intra-section reorder.
            """
            m = memory_engine.get(name)
            if m is None:
                return json.dumps({"error": f"memory not found: {name}"})
            if usage is not None:
                usage.record_open(name)
            return json.dumps(m.model_dump(mode="json"), indent=2, ensure_ascii=False)

        @mcp.tool()
        def list_memories(type: str | None = None, tier: str | None = None) -> str:
            """List memory names + descriptions, optionally filtered by type
            (user/feedback/project/reference) and/or tier (user/project)."""
            ms = memory_engine.list(type=type, tier=tier)  # type: ignore[arg-type]
            return json.dumps(
                [_memory_summary(m) for m in ms], indent=2, ensure_ascii=False
            )

        @mcp.tool()
        def memory_lint() -> str:
            """Hygiene report over the memory store — broken [[links]], index
            orphans, stale index pointers. The raw material a /dream
            consolidation pass acts on."""
            return json.dumps(memory_engine.lint(), indent=2, ensure_ascii=False)

        @mcp.tool()
        def memory_usage(limit: int = 50) -> str:
            """Hot -> cold usage report (frecency): opens, recalls, last-access
            timestamps and a heat score per memory, hottest first.

            This is the signal /dream uses to reorder bullets WITHIN each
            MEMORY.md section (section grouping stays human-curated; intra-section
            order becomes warmth-driven). Frequency-dominant — wall-clock pauses
            do not cool a memory. Counts only explicit recall()/get_memory()
            hits; the native auto-recall bypasses this layer, so figures are a
            lower bound, not a full tally."""
            if usage is None:
                return json.dumps({"error": "usage tracking unavailable"})
            return json.dumps(usage.report()[:limit], indent=2, ensure_ascii=False)

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

    # Auto-write fallback markdown so there's always a plain-text reference
    # for the MCP-outage case. Silent on failure — fallback is nice-to-have.
    try:
        fallback_target = rules_dir / "_meta" / "fallback_rules.md"
        stats = engine.write_fallback_markdown(fallback_target)
        print(
            f"[claude-rules] wrote fallback with {stats['written']} critical rules to "
            f"{fallback_target}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[claude-rules] fallback write skipped: {e}", file=sys.stderr)

    # Memory store is optional — if found, the same MCP also serves recall /
    # get_memory / list_memories / memory_lint. If not, rules-only as before.
    memory_engine = None
    usage = None
    mem_dir = _discover_memory_dir()
    if mem_dir is not None:
        memory_engine = MemoryEngine()
        memory_engine.load_directory(mem_dir, tier="project", strict=False)
        user_mem = os.environ.get("CLAUDE_USER_MEMORY_DIR")
        if user_mem and Path(user_mem).expanduser().is_dir():
            memory_engine.load_directory(user_mem, tier="user", strict=False)
        # Hot/cold (frecency) signal — sidecar in the PROJECT memory dir.
        usage = UsageStore.for_memory_dir(mem_dir)
        print(
            f"[claude-rules] loaded {len(memory_engine.memories)} memories from "
            f"{mem_dir}" + (f" + user tier {user_mem}" if user_mem else ""),
            file=sys.stderr,
        )

    server = build_server(engine, memory_engine, usage)
    server.run()


if __name__ == "__main__":
    main()
