"""Memory MCP tools."""

from __future__ import annotations

import datetime
import json
import math

from mcp_context_toolkit.core import clamp_limit

from .serializers import _memory_summary


def register(mcp_tool, memory_reloader, usage=None) -> None:
    """Register the memory-domain tools. Only called when a memory store was
    found — `usage` (frecency sidecar) is independently optional."""

    @mcp_tool("memory")
    def recall(query: str, limit: int = 8) -> str:
        """Recall the most relevant memories for a query, ranked across BOTH
        tiers (project + user). Keyword-based, deterministic, hot/cold-aware.

        A host's own auto-memory (if any) typically surfaces only the project
        tier — use this to pull project AND user-global knowledge on demand.
        Ranking blends
        keyword relevance with frecency (frequently/recently used memories
        are pulled forward) and **backlink structure** (memories cited by
        many others rise naturally). Returns summaries; follow up with
        get_memory(name) for the full body — which also marks it as used.

        Backlink boost: log-dampened, ``log1p(inbound_links) * 0.1`` —
        a memory with 10 inbound links gets ~+0.24 to its score, so
        structurally important knowledge surfaces without explicit usage
        while the boost never dominates the base relevance.
        """
        boosts = usage.boosts() if usage is not None else {}
        # ONE engine snapshot for both the graph and the ranking: calling
        # current() twice could straddle a reload, scoring hits from one
        # store against link counts from another.
        engine = memory_reloader.current()
        # Backlink boost from graph: {name: log1p(inbound_count) * 0.1}
        # log1p dampens the effect for highly-cited memories, keeps it
        # proportional but bounded.
        backlink_boosts = {}
        inbound: dict[str, int] = {}
        for _src, tgt in engine.edges():
            inbound[tgt] = inbound.get(tgt, 0) + 1
        for name, count in inbound.items():
            # log1p(x) * 0.1 → 1 link ≈ +0.07, 10 ≈ +0.24, 100 ≈ +0.46:
            # grows sub-linearly, never dominates the base relevance score.
            backlink_boosts[name] = math.log1p(count) * 0.1
        hits = engine.recall(
            query, limit=limit, boost=boosts, backlink_boost=backlink_boosts
        )
        if usage is not None:
            usage.record_recall([m.name for m in hits])
        return json.dumps(
            [_memory_summary(m) for m in hits], indent=2, ensure_ascii=False
        )

    @mcp_tool("memory")
    def get_memory(name: str) -> str:
        """Fetch the full body + metadata of a single memory by its name.

        Counts as a strong usage hit (heavier than a recall appearance) —
        this is what makes a memory 'hot' and pulls it forward in future
        recall results and in a consolidation pass's intra-section reorder.
        """
        engine = memory_reloader.current()
        m = engine.get(name)
        if m is None:
            return json.dumps({"error": f"memory not found: {name}"})
        if usage is not None:
            usage.record_open(name)
        payload = m.model_dump(mode="json")
        # Inbound edges: which memories link here (the reverse of `links`).
        # Resolved to the canonical name, so a link via a bundled member slug
        # is credited to `m` if `m` is the package. Cheap graph context for
        # navigation + orphan-spotting without a separate memory_lint call.
        payload["cited_by"] = engine.backlinks(m.name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def list_memories(type: str | None = None, tier: str | None = None) -> str:
        """List memory names + descriptions, optionally filtered by type
        (user/feedback/project/reference) and/or tier (user/project)."""
        ms = memory_reloader.current().list(type=type, tier=tier)  # type: ignore[arg-type]
        return json.dumps(
            [_memory_summary(m) for m in ms], indent=2, ensure_ascii=False
        )

    @mcp_tool("memory")
    def memory_dream_status(
        files_threshold: int = 3,
        lint_threshold: int = 2,
    ) -> str:
        """Check if the memory store needs consolidation (dream).

        Returns a status report with:
        - files_changed_since_last_dream: N memory files modified within the
          recent-change window (7-day mtime heuristic — see note below)
        - lint_issues: {broken_links: X, orphans: Y, stale_pointers: Z}
        - recommendation: "dream fällig" or "kein dringender Bedarf"

        Thresholds are configurable:
        - files_threshold: trigger if N+ files changed since last dream
        - lint_threshold: trigger if N+ lint issues exist

        This is the trigger for `/context-dream` — when the store grows
        raw (many changes, broken links, orphans) it's time to consolidate
        (merge dupes, compress, repair links, normalize names). Always
        git-backed/revertible.

        Note: "since last dream" is approximated by a 7-day mtime window,
        not a tracked last-run timestamp. A memory file modified in the
        last 7 days counts as "changed"; older files are assumed
        consolidated. This keeps the tool stateless (no marker file) at
        the cost of over-counting after a quiet week.
        """
        engine = memory_reloader.current()

        # Get lint report
        lint = engine.lint()

        # Count lint issues
        broken_links = sum(
            len(missing) for missing in lint.get("broken_links", {}).values()
        )
        orphans_count = len(lint.get("orphans", []))
        stale_pointers = len(lint.get("stale_pointers", []))
        total_lint_issues = broken_links + orphans_count + stale_pointers

        # Count recently-changed memory files (7-day mtime heuristic — see
        # the docstring note: this is a stateless proxy for "since last
        # dream", not a tracked last-run timestamp).
        _RECENT_DAYS = 7
        _INTERNAL_FILES = {"MEMORY.md", "_descriptions.md"}
        now = datetime.datetime.now()
        files_changed = 0
        for memory_root in memory_reloader.watch_dirs:
            try:
                for md in memory_root.rglob("*.md"):
                    if md.name in _INTERNAL_FILES or md.name.startswith("_"):
                        continue
                    file_mtime = datetime.datetime.fromtimestamp(md.stat().st_mtime)
                    if (now - file_mtime).days <= _RECENT_DAYS:
                        files_changed += 1
            except OSError:
                pass

        # Determine recommendation
        if files_changed >= files_threshold or total_lint_issues >= lint_threshold:
            recommendation = "🧠 dream fällig"
        elif files_changed > 0 or total_lint_issues > 0:
            recommendation = "💡 dream empfohlen"
        else:
            recommendation = "✅ kein dringender Bedarf"

        status = {
            "files_changed_since_last_dream": files_changed,
            "lint_issues": lint,
            "broken_links": broken_links,
            "orphans": orphans_count,
            "stale_pointers": stale_pointers,
            "total_lint_issues": total_lint_issues,
            "recommendation": recommendation,
            "thresholds": {
                "files_threshold": files_threshold,
                "lint_threshold": lint_threshold,
            },
        }
        return json.dumps(status, indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def memory_lint() -> str:
        """Hygiene report over the memory store — broken [[links]], index
        orphans, stale index pointers. The raw material a consolidation
        pass acts on."""
        return json.dumps(
            memory_reloader.current().lint(), indent=2, ensure_ascii=False
        )

    @mcp_tool("memory")
    def memory_usage(limit: int = 50) -> str:
        """Hot -> cold usage report (frecency): opens, recalls, last-access
        timestamps and a heat score per memory, hottest first.

        This is the signal a consolidation pass uses to reorder bullets WITHIN
        each MEMORY.md section (grouping stays human-curated; intra-section
        order becomes warmth-driven). Frequency-dominant — wall-clock pauses
        do not cool a memory. Counts only explicit recall()/get_memory()
        hits; a host's own auto-recall bypasses this layer, so figures are a
        lower bound, not a full tally.

        `limit` is clamped via `core.clamp_limit` — same reasoning as
        recall()'s: an unclamped negative value is a valid, reversed slice."""
        if usage is None:
            return json.dumps({"error": "usage tracking unavailable"})
        return json.dumps(usage.report()[:clamp_limit(limit)], indent=2, ensure_ascii=False)
