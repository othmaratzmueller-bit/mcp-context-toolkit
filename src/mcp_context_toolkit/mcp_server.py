from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.usage import UsageStore

# Store-directory conventions tried during walk-up auto-discovery, in order.
# `.context` is the generic default (matches the tool/env naming); `.claude`
# is kept as a fallback so a Claude Code repo with an existing .claude/rules
# store keeps working without configuration.
_STORE_CONVENTIONS = (".context", ".claude")


def _discover_rules_dir() -> Path:
    env = os.environ.get("CONTEXT_RULES_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in _STORE_CONVENTIONS:
            rules = candidate / conv / "rules"
            if rules.is_dir():
                return rules

    raise FileNotFoundError(
        "No rules directory found. Set CONTEXT_RULES_DIR or create .context/rules/ "
        "(or .claude/rules/) in the current working tree."
    )


def _discover_memory_dir() -> Path | None:
    """Locate the PROJECT memory directory. Env CONTEXT_MEMORY_DIR wins, else
    walk up for .context/memory (or .claude/memory). Returns None if none found —
    memory tools are then simply not registered and rules keep working untouched."""
    env = os.environ.get("CONTEXT_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in _STORE_CONVENTIONS:
            mem = candidate / conv / "memory"
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


def _tree_mtime(root: Path) -> float:
    """Highest mtime across a store tree (the dir itself + every file/subdir).

    Walking the whole tree — not just the dir's own mtime — is deliberate:
    editing an existing file bumps only that file's mtime, while add/remove
    bumps the dir's. Taking the max over everything catches both, so an in-place
    edit is seen as well as a freshly added memory file. 0.0 if the tree is gone.
    """
    try:
        latest = root.stat().st_mtime
    except OSError:
        return 0.0
    for p in root.rglob("*"):
        try:
            mt = p.stat().st_mtime
        except OSError:
            continue  # racing deletion etc. — skip, best-effort
        if mt > latest:
            latest = mt
    return latest


class _Reloader:
    """Holds a built engine + the dirs it was loaded from, and rebuilds it (via
    the supplied loader) only when those trees' mtime changes.

    A long-running MCP process loads its store once at start; without this an
    edit or a consolidation re-bundle from a parallel session stays invisible
    until restart. With it, the next tool call sees the change — a cheap mtime scan,
    reload only on a real change. The engines stay pure-read; the freshness
    concern lives here in the server layer.
    """

    def __init__(self, loader, watch_dirs):
        self._loader = loader
        self._watch = [Path(d) for d in watch_dirs if d is not None]
        self._mtime = self._scan()
        self._engine = self._loader()

    def _scan(self) -> float:
        return max((_tree_mtime(d) for d in self._watch), default=0.0)

    def current(self):
        """Return the engine, rebuilding first iff the watched trees changed."""
        mt = self._scan()
        if mt != self._mtime:
            self._mtime = mt
            self._engine = self._loader()
        return self._engine


def build_server(
    rules_reloader: "_Reloader",
    memory_reloader: "_Reloader | None" = None,
    usage: UsageStore | None = None,
):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("context")

    @mcp.tool()
    def query_rules_for_file(file_path: str) -> str:
        """Return codebase intelligence context (Rules, Decisions, Dependencies) for the given file path.

        Use this as the first action when opening a file in a session —
        it filters the repo knowledge down to the relevant context for this file.
        """
        engine = rules_reloader.current()
        matches = engine.query_for_file(file_path)
        rules_out = [_rule_summary(r) for r in matches]

        decisions = engine.query_decisions_for_file(file_path)
        decisions_out = [d.model_dump(mode="json") for d in decisions]

        deps = engine.query_dependencies(file_path)

        out = {
            "rules": rules_out,
            "decisions": decisions_out,
            "dependencies": deps
        }
        return json.dumps(out, indent=2)

    @mcp.tool()
    def get_rule(key: str) -> str:
        """Fetch the full content of a single rule by key."""
        rule = rules_reloader.current().get_rule(key)
        if rule is None:
            return json.dumps({"error": f"rule not found: {key}"})
        return json.dumps(_rule_full(rule), indent=2, default=str)

    @mcp.tool()
    def list_rule_keys(type: str | None = None, scope: str | None = None) -> str:
        """List all loaded rule keys, optionally filtered by type and/or scope."""
        keys = rules_reloader.current().list_keys(type=type, scope=scope)  # type: ignore[arg-type]
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
        engine = rules_reloader.current()
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
          - A specific module: query_rules(module="billing")
        """
        matches = rules_reloader.current().query(
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
    if memory_reloader is not None:

        @mcp.tool()
        def recall(query: str, limit: int = 8) -> str:
            """Recall the most relevant memories for a query, ranked across BOTH
            tiers (project + user). Keyword-based, deterministic, hot/cold-aware.

            A host's own auto-memory (if any) typically surfaces only the project
            tier — use this to pull project AND user-global knowledge on demand.
            Ranking blends
            keyword relevance with frecency (frequently/recently used memories
            are pulled forward). Returns summaries; follow up with
            get_memory(name) for the full body — which also marks it as used.
            """
            boosts = usage.boosts() if usage is not None else {}
            hits = memory_reloader.current().recall(query, limit=limit, boost=boosts)
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
            recall results and in a consolidation pass's intra-section reorder.
            """
            m = memory_reloader.current().get(name)
            if m is None:
                return json.dumps({"error": f"memory not found: {name}"})
            if usage is not None:
                usage.record_open(name)
            return json.dumps(m.model_dump(mode="json"), indent=2, ensure_ascii=False)

        @mcp.tool()
        def list_memories(type: str | None = None, tier: str | None = None) -> str:
            """List memory names + descriptions, optionally filtered by type
            (user/feedback/project/reference) and/or tier (user/project)."""
            ms = memory_reloader.current().list(type=type, tier=tier)  # type: ignore[arg-type]
            return json.dumps(
                [_memory_summary(m) for m in ms], indent=2, ensure_ascii=False
            )

        @mcp.tool()
        def memory_lint() -> str:
            """Hygiene report over the memory store — broken [[links]], index
            orphans, stale index pointers. The raw material a consolidation
            pass acts on."""
            return json.dumps(
                memory_reloader.current().lint(), indent=2, ensure_ascii=False
            )

        @mcp.tool()
        def memory_usage(limit: int = 50) -> str:
            """Hot -> cold usage report (frecency): opens, recalls, last-access
            timestamps and a heat score per memory, hottest first.

            This is the signal a consolidation pass uses to reorder bullets WITHIN
            each MEMORY.md section (grouping stays human-curated; intra-section
            order becomes warmth-driven). Frequency-dominant — wall-clock pauses
            do not cool a memory. Counts only explicit recall()/get_memory()
            hits; a host's own auto-recall bypasses this layer, so figures are a
            lower bound, not a full tally."""
            if usage is None:
                return json.dumps({"error": "usage tracking unavailable"})
            return json.dumps(usage.report()[:limit], indent=2, ensure_ascii=False)

    return mcp


# The "big sign" shown on server start. Attribution is deliberate — this is a
# named tool; forks are free to change AUTHOR.
TOOLKIT_NAME = "mcp-context-toolkit"
AUTHOR = "Othmar Atzmüller"
AUTHOR_URL = "github.com/othmaratzmueller-bit"


def _print_banner(engine: RulesEngine, memory_engine: MemoryEngine | None) -> None:
    """Print the startup banner to stderr — every MCP client that launches the
    server sees it (generic, not tied to any one client). Lists what is loaded
    and credits the author."""
    from collections import Counter

    rule_types = Counter(r.type for r in engine.rules)
    rule_breakdown = " · ".join(
        f"{n} {t}" for t, n in sorted(rule_types.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    data = [("Rules", len(engine.rules), rule_breakdown or "—")]
    if memory_engine is not None:
        tiers = Counter(m.tier for m in memory_engine.memories)
        tier_breakdown = " + ".join(
            t for t, _ in sorted(tiers.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        data.append((
            "Memory", len(memory_engine.memories),
            f"{tier_breakdown} · frecency-ranked" if tier_breakdown else "—",
        ))
        data.append(("Tools", 10, "5 rules · 5 memory"))
    else:
        data.append(("Memory", 0, "(no store found)"))
        data.append(("Tools", 5, "5 rules"))

    bar = "─" * 64
    out = [
        "",
        bar,
        f" 🧰  {TOOLKIT_NAME}  ·  the right context, on demand",
        f"     by {AUTHOR} · {AUTHOR_URL}",
        bar,
    ]
    out += [f" {label:<7}{count:>4}   {detail}" for label, count, detail in data]
    out += [bar, ""]
    print("\n".join(out), file=sys.stderr)


def main() -> None:
    try:
        rules_dir = _discover_rules_dir()
    except FileNotFoundError as e:
        print(f"[context-toolkit] {e}", file=sys.stderr)
        sys.exit(2)

    # The shipped starter pack (examples/rules) is inert — copy-to-activate. If a
    # misconfigured CONTEXT_RULES_DIR points at it directly, say so loudly so the
    # examples don't silently become someone's production rule set.
    if "examples" in rules_dir.parts:
        print(
            f"[context-toolkit] NOTE: serving EXAMPLE/starter rules from {rules_dir} "
            f"— inert starter pack, copy into your own rules dir for real use.",
            file=sys.stderr,
        )

    # Wrap the rules engine in a reloader so an in-session YAML edit (or a
    # parallel session's) is picked up at the next tool call, not just at restart.
    decisions_dir = rules_dir.parent / "decisions"
    watch_dirs = [rules_dir]
    if decisions_dir.is_dir():
        watch_dirs.append(decisions_dir)
    rules_reloader = _Reloader(lambda: RulesEngine.from_directory(rules_dir), watch_dirs)
    engine = rules_reloader.current()  # initial build (no reload yet)

    # Auto-write fallback markdown so there's always a plain-text reference
    # for the MCP-outage case. Silent on failure — fallback is nice-to-have.
    try:
        fallback_target = rules_dir / "_meta" / "fallback_rules.md"
        stats = engine.write_fallback_markdown(fallback_target)
        print(
            f"[context-toolkit] wrote fallback with {stats['written']} critical rules to "
            f"{fallback_target}",
            file=sys.stderr,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[context-toolkit] fallback write skipped: {e}", file=sys.stderr)

    # Memory store is optional — if found, the same MCP also serves recall /
    # get_memory / list_memories / memory_lint. If not, rules-only as before.
    # The memory engine is likewise wrapped in a reloader so a consolidation
    # rebundle from a parallel session refreshes this process at its next call.
    memory_reloader = None
    usage = None
    mem_dir = _discover_memory_dir()
    if mem_dir is not None:
        user_mem = os.environ.get("CONTEXT_USER_MEMORY_DIR")
        user_mem_dir = Path(user_mem).expanduser() if user_mem else None
        if user_mem_dir is not None and not user_mem_dir.is_dir():
            user_mem_dir = None

        def _load_memory() -> MemoryEngine:
            me = MemoryEngine()
            me.load_directory(mem_dir, tier="project", strict=False)
            if user_mem_dir is not None:
                me.load_directory(user_mem_dir, tier="user", strict=False)
            return me

        memory_reloader = _Reloader(_load_memory, [mem_dir, user_mem_dir])
        # Hot/cold (frecency) signal — sidecar in the PROJECT memory dir.
        usage = UsageStore.for_memory_dir(mem_dir)

    memory_engine = memory_reloader.current() if memory_reloader is not None else None
    _print_banner(engine, memory_engine)
    server = build_server(rules_reloader, memory_reloader, usage)
    server.run()


if __name__ == "__main__":
    main()
