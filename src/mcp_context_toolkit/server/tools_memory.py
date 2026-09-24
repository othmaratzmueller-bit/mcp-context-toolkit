"""Memory MCP tools."""

from __future__ import annotations

import datetime
import json

from mcp_context_toolkit.core import clamp_limit
from mcp_context_toolkit.memory import memory_recall_hits

from .serializers import _memory_summary


def register(mcp_tool, memory_reloader, usage=None, event_log=None) -> None:
    """Register the memory-domain tools. Only called when a memory store was
    found — `usage` (frecency sidecar) and `event_log` (Etappe 0 recall
    observability, `RecallEventLog`) are independently optional."""

    @mcp_tool("memory")
    def recall(
        query: str,
        limit: int = 8,
        bausteine: list[str] | None = None,
        themen: list[str] | None = None,
        taetigkeiten: list[str] | None = None,
    ) -> str:
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

        ``bausteine``, ``themen`` and ``taetigkeiten`` are optional curated
        context axes; values within an axis are OR alternatives. Context can
        add at most two candidates into otherwise unused slots, never replace
        a keyword hit, and shares recall's global result limit.
        """
        engine = memory_reloader.current()
        hits = memory_recall_hits(
            engine, query, limit=limit, usage=usage, event_log=event_log,
            bausteine=bausteine, themen=themen, taetigkeiten=taetigkeiten,
        )
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
        if event_log is not None:
            event_log.log_opened(name)
        payload = m.model_dump(mode="json")
        # Inbound edges: which memories link here (the reverse of `links`).
        # Resolved to the canonical name, so a link via a bundled member slug
        # is credited to `m` if `m` is the package. Cheap graph context for
        # navigation + orphan-spotting without a separate memory_lint call.
        payload["cited_by"] = engine.backlinks(m.name)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def memory_context_candidates(
        bausteine: list[str] | None = None,
        themen: list[str] | None = None,
        taetigkeiten: list[str] | None = None,
    ) -> str:
        """Show curated context candidates without changing recall ranking.

        Values within an axis are alternatives. The response records every
        matching axis/value so a caller can inspect a future recall lane before
        it is allowed to influence an injected result.
        """
        engine = memory_reloader.current()
        matches = engine.context_candidates(
            bausteine=bausteine, themen=themen, taetigkeiten=taetigkeiten,
        )
        return json.dumps(
            [{**_memory_summary(memory), "matches": axes} for memory, axes in matches],
            indent=2, ensure_ascii=False,
        )

    @mcp_tool("memory")
    def memory_recall_explain(
        query: str,
        limit: int = 8,
        bausteine: list[str] | None = None,
        themen: list[str] | None = None,
        taetigkeiten: list[str] | None = None,
    ) -> str:
        """Explain keyword and curated-context candidates without warming them."""
        engine = memory_reloader.current()

        class _ReadOnlyUsage:
            def boosts(self):
                return usage.boosts() if usage is not None else {}

            def record_recall(self, names):
                return None

        read_only_usage = _ReadOnlyUsage()
        baseline = memory_recall_hits(engine, query, limit=limit, usage=read_only_usage)
        final = memory_recall_hits(
            engine, query, limit=limit, usage=read_only_usage,
            bausteine=bausteine, themen=themen, taetigkeiten=taetigkeiten,
        )
        context_by_name = {
            memory.name: axes for memory, axes in engine.context_candidates(
                bausteine=bausteine, themen=themen, taetigkeiten=taetigkeiten,
            )
        }
        baseline_names = {memory.name for memory in baseline}
        rows = []
        for memory in final:
            sources = ["keyword"] if memory.name in baseline_names else []
            if memory.name in context_by_name:
                sources.append("context")
            rows.append({**_memory_summary(memory), "sources": sources,
                         "context_matches": context_by_name.get(memory.name, {})})
        return json.dumps({"query": query, "baseline": [m.name for m in baseline],
                           "candidates": rows}, indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def learn_recall_gap(
        query: str,
        target_name: str,
        bridge_terms: list[str],
        scout_names: list[str],
        mechanical_names: list[str] | None = None,
        limit: int = 6,
        turn: int | None = None,
    ) -> str:
        """Teach the mechanical recall a gap a scout found — the ONLY memory
        tool that writes.

        Call after a scout (a small model searching with its own vocabulary)
        found ``target_name`` for ``query`` where recall did not. The engine
        recomputes the ranking (no session dedupe), requires the target to be
        a real miss and named by the scout, requires every bridge term to
        reach the query, replays the terms as extra ``learned_tags`` through
        the same ranking, and writes only the minimal subset that lifts the
        target into the top ``limit`` into ``learned_tags`` (ranked like
        ``tags``, never touching them). Returns JSON with ``status``
        (accepted/noop/rejected), ``reason`` and before/after evidence. Starts
        no model and no subprocess."""
        from mcp_context_toolkit.recall_learning import learn_recall_gap as _learn

        result = _learn(
            memory_reloader.current(), query=query, target_name=target_name,
            bridge_terms=bridge_terms, scout_names=scout_names,
            mechanical_names=mechanical_names, limit=limit,
            boosts=usage.boosts() if usage is not None else {},
            event_log=event_log, turn=turn,
        )
        return json.dumps(result.model_dump(), indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def complete_scout_comparison(
        query: str,
        turn: int,
        limit: int,
        mechanical_names: list[str],
        scout_hits: list[dict],
        duration_ms: int = 0,
    ) -> str:
        """Atomically finish the learning from one scout comparison.

        Supply every scout hit as ``{"target_name": ..., "bridge_terms": [...]}``
        in this ONE call after the small model searched the same query. The
        core records a resumable per-session/turn transaction, validates every
        hit before it can write, and deduplicates retries. Use this instead of
        separately logging a comparison and calling ``learn_recall_gap`` per
        target; the latter remains available for diagnosis and compatibility.
        """
        from mcp_context_toolkit.recall_learning import complete_scout_comparison as _complete

        result = _complete(
            memory_reloader.current(), query=query, turn=turn, limit=limit,
            mechanical_names=mechanical_names, scout_hits=scout_hits,
            duration_ms=duration_ms, boosts=usage.boosts() if usage is not None else {},
            event_log=event_log,
        )
        return json.dumps(result.model_dump(), indent=2, ensure_ascii=False)

    @mcp_tool("memory")
    def preview_learned_tag_ablation() -> str:
        """Read-only maintenance evidence for every learned recall tag.

        Replays only queries from accepted, durably claimed scout comparisons,
        first with the current tag set and then with each learned tag removed.
        `necessary` means a recorded replay would miss its original limit;
        `candidate` means every available replay still meets it; and
        `not_decidable` means the evidence is absent or the current state no
        longer reproduces it. The tool never edits a memory file.
        """
        from mcp_context_toolkit.recall_learning import preview_learned_tag_ablation as _preview

        result = _preview(
            memory_reloader.current(), event_log=event_log,
            boosts=usage.boosts() if usage is not None else {},
        )
        return json.dumps(result.model_dump(), indent=2, ensure_ascii=False)

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
