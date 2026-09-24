"""Work-item MCP tools."""

from __future__ import annotations

import json

from mcp_context_toolkit.host_adapters import discover_host_adapter
from mcp_context_toolkit.work import RECALL_DEFAULT_EXCLUDED_KINDS, work_status_payload

from .serializers import _work_summary

# WorkStatus's full vocabulary (models.py) — spelled out rather than
# imported so this stays a plain counting dict, not a dependency on the
# model's Literal type at the server layer.
_ALL_STATUSES = ("open", "active", "done", "dropped", "superseded")

# Total response budget for list_work's item-list path (the topic overview
# above has its own, much smaller, natural size). A single FILTERED value
# is not a reliable size bound on this store: `status="done"` alone
# measured ~926K characters (~231K tokens) on the real TALOS work store —
# larger than the unfiltered case this etappe just capped, and bigger than
# any reasonable one-call budget. This binds the response regardless of
# which filter (or none) produced it, the same way `_DOC_SNIPPET_CHARS`
# binds a single docs_recall hit regardless of the source section's size.
_LIST_WORK_MAX_CHARS = 60_000


def _wrapped(rows: list[dict], omitted: int) -> str:
    return json.dumps({
        "items": rows,
        "truncated": True,
        "omitted": omitted,
        "hint": (f"Response budget ({_LIST_WORK_MAX_CHARS} chars) exceeded — "
                 f"{omitted} item(s) omitted. Narrow with "
                 f"theme=/status=/kind= to see the rest."),
    }, indent=2, ensure_ascii=False)


def _cap_response(rows: list[dict]) -> str:
    """Serialize `rows`, truncating (with a `truncated`/`omitted` marker)
    if the full list would exceed `_LIST_WORK_MAX_CHARS`. Shrinks in 10%
    steps rather than computing an exact cutoff — this runs once per tool
    call, not a hot path, and an approximate budget that is simple to
    reason about beats an exact one that risks an off-by-one over it.

    The loop measures the WRAPPED response (`_wrapped`), not the bare
    `rows` list — a first version measured the unwrapped array while
    returning the wrapped object, whose `items` key nests every row one
    level deeper (2 extra indent characters per line under `indent=2`)
    plus the three marker fields. Measured on the real store: `kind="step"`
    (216 kept items) came out at 63,389 characters against a 60,000
    budget — over it by exactly the wrapping overhead the loop never
    accounted for."""
    serialized = json.dumps(rows, indent=2, ensure_ascii=False)
    if len(serialized) <= _LIST_WORK_MAX_CHARS or not rows:
        return serialized
    kept = rows
    while len(kept) > 1 and len(_wrapped(kept, len(rows) - len(kept))) > _LIST_WORK_MAX_CHARS:
        kept = kept[: max(1, len(kept) * 9 // 10)]
    return _wrapped(kept, len(rows) - len(kept))


def _topic_overview(engine, *, include_archived: bool) -> dict:
    """Per-theme counts instead of every item — see list_work's docstring
    for why this exists (an unfiltered listing has no natural size bound,
    live-backlog or fully-archived alike). Grouping is the same `theme`
    field `list_work` already filters on, so `theme=<name>` from this
    overview reaches the same items a filtered call would return.
    `include_archived` is forwarded as-is — the overview counts exactly
    the items the equivalent filtered call would have seen, live backlog
    or full archive, never silently widening the scope of what a plain
    `list_work()` (no `include_archived`) call is allowed to count.

    With `include_archived=False`, `done`/`dropped`/`superseded` are
    OMITTED from each topic entirely, not reported as 0 — a fresh-context
    review measured every topic showing zeroes there in the live-backlog
    case even for themes that actually have hundreds of closed items
    (e.g. 152 done items in one theme), because the closed ones were
    filtered out before counting. A reader sees only the payload, not this
    docstring; "done: 0" reads as "no completed work", which is false.
    Omitting the keys makes the scope visible in the shape itself, not
    just in prose."""
    counted_statuses = _ALL_STATUSES if include_archived else ("open", "active")
    by_theme: dict[str, dict[str, int]] = {}
    for item in engine.list(include_archived=include_archived):
        row = by_theme.setdefault(
            item.theme, {"total": 0, **dict.fromkeys(counted_statuses, 0)}
        )
        row["total"] += 1
        row[item.status] += 1
    topics = [{"theme": theme, **counts}
              for theme, counts in sorted(by_theme.items())]
    scope = ("the live backlog only (open/active) — closed items are not "
             "counted here" if not include_archived else
             "the full archive, all statuses")
    return {
        "topics": topics,
        "hint": f"Full item list omitted (an unfiltered call has no natural "
                f"size bound) — counts cover {scope}. Call "
                f"list_work(theme=<name>) to see the actual items for one "
                f"topic.",
    }


def register(mcp_tool, work_reloader, work_dir=None) -> None:
    """Register the work-domain tools. `work_dir` is used only by
    `work_lint(check_symbols=True)` to build a host adapter for symbol
    resolution — absent it, `check_symbols` is accepted but never finds an
    index (same as no adapter installed)."""

    @mcp_tool("work")
    def work_recall(query: str, limit: int = 8) -> str:
        """Find work items (plans/roadmaps as records) by keyword, ranked
        BM25F over title + body — same ranking algorithm as memory's
        recall(), via the shared core.bm25_scores. No frecency/backlink
        boost (unlike memory): nothing in this store's design asked for a
        'hot work item' concept (W20 — ask before inventing).

        Use this to find an existing item before creating a new one — the
        store's whole purpose is one record per idea, not a duplicate
        because the existing one wasn't found.

        Excludes `kind: changelog` (Etappe D, W8-Reviewer-Fund ERROR-2,
        narrowed in the delta round per ERROR-B): a changelog line is
        never "an existing item to reuse" — after the changelog backfill
        it crowded out the goal/step/note records this search exists to
        find. `kind: decision` stays IN the pool on purpose — a decision
        record answers exactly the W20/S8 question ("was this already
        considered?"), and Etappe C folded decisions into this store
        specifically so they would stay findable here; excluding them
        too undid that (measured: up to 5/8 top slots lost on a
        decision-heavy query, for zero benefit — the pollution ERROR-2
        found was 100% changelog, 0% decision)."""
        engine = work_reloader.current()
        hits = engine.recall(query, limit=limit,
                             exclude_kinds=RECALL_DEFAULT_EXCLUDED_KINDS)
        return json.dumps(
            [_work_summary(w) for w in hits], indent=2, ensure_ascii=False
        )

    @mcp_tool("work")
    def get_work(key: str) -> str:
        """Fetch a single work item (plan/roadmap record) by key, with its
        x/y rollup if it has children. Counts are COMPUTED from the
        children's status, never stored — a goal cannot silently claim a
        progress its children no longer show."""
        engine = work_reloader.current()
        item = engine.get(key)
        if item is None:
            return json.dumps({"error": f"work item not found: {key}"})
        payload = item.model_dump(mode="json")
        payload["rollup"] = engine.rollup(key)
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @mcp_tool("work")
    def list_work(
        theme: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        category: str | None = None,
        include_archived: bool = False,
    ) -> str:
        """List work items, optionally filtered by theme/status/kind/category.
        Default view is the LIVE backlog — `done`/`dropped`/`superseded`
        are excluded unless `status` names one of them explicitly or
        `include_archived=True` is passed (closed items are never
        deleted, just hidden from the default "what's open" view).
        Each row includes an x/y rollup when the item has children —
        computed on the fly, never a maintained field.

        `category` (bug/feature/debt/research) is the axis `theme` (domain)
        and `kind` (granularity) don't cover: "what KIND of work is this" —
        e.g. `list_work(category="bug")` for open problems across every
        theme, without guessing from titles. `None` on an item means
        genuinely uncategorized (not yet triaged), not a fifth value to
        filter on.

        Any call with NO theme/status/kind/category filter has no natural
        size bound — even the plain default (live backlog, no
        `include_archived`) measured at 1272 items serializes to ~91K
        tokens in one response, and the fully-archived case (5124 items)
        to ~326K, larger than the docs_recall incident (Etappe 7) that
        closed the same gap for the recall-style tools. An unfiltered call
        returns a per-theme topic overview instead of the raw dump, with a
        hint to narrow with `theme=` — `include_archived` still controls
        whether the overview itself counts the live backlog or the full
        archive.

        A FILTERED call is not automatically safe either — a single value
        is not a reliable size bound on a large store: `status="done"`
        alone measured ~231K tokens on the real TALOS work store, bigger
        than the unfiltered case above. Every item-list response (filtered
        or not) therefore shares one response budget (see
        `_LIST_WORK_MAX_CHARS`); once exceeded, the result is capped with
        a `truncated`/`omitted` marker rather than growing without limit."""
        engine = work_reloader.current()
        if theme is None and status is None and kind is None and category is None:
            return json.dumps(
                _topic_overview(engine, include_archived=include_archived),
                indent=2, ensure_ascii=False,
            )
        items = engine.list(
            theme=theme, status=status, kind=kind, category=category,
            include_archived=include_archived,
        )
        out = []
        for w in items:
            row = _work_summary(w)
            roll = engine.rollup(w.key)
            if roll["total"]:
                row["rollup"] = roll
            out.append(row)
        return _cap_response(out)

    @mcp_tool("work")
    def work_children(key: str) -> str:
        """Direct children of a goal via `part_of` — the hierarchy IS this
        relation, not a fixed level. A leaf work item (the median case on
        a real backlog) simply has none."""
        engine = work_reloader.current()
        return json.dumps(
            [_work_summary(w) for w in engine.children(key)],
            indent=2, ensure_ascii=False,
        )

    @mcp_tool("work")
    def work_lint(check_symbols: bool = False) -> str:
        """Hygiene report over the work store: dangling part_of/amends/
        supersedes/blocked_by references, goals whose children are ALL
        closed but the goal itself is not (the exact "recommended closing
        it, nobody did" failure mode this store was built to catch),
        done items with no evidence, and half-closed status/date pairs.

        `check_symbols=True` additionally resolves every `uses`/
        `extends`/`creates`/`modules` anchor against the real symbol tree
        (`broken_reuse_refs` in the result) — off by default because it
        costs a full tree scan (~1.7s measured); ask for it explicitly
        when you actually need to know whether recorded anchors still
        hold, not on every routine lint call."""
        symbol_check = None
        if check_symbols and work_dir is not None:
            adapter = discover_host_adapter(work_dir)
            if adapter.has_symbol_index():
                symbol_check = adapter.resolve_symbol
        return json.dumps(
            work_reloader.current().lint(symbol_check), indent=2, ensure_ascii=False
        )

    @mcp_tool("work")
    def work_status() -> str:
        """Check whether the work store needs attention — the work
        counterpart to memory_dream_status. Reports how many goals are
        closeable right now, how many references are dangling, how many
        done items lack evidence, and how many files were DROPPED at load
        (invalid schema, duplicate key), with a plain recommendation.

        load_errors gets top billing in the recommendation, not just a
        tally: a dropped record can make an UNRELATED goal look falsely
        closeable (its only still-open child never made it into the
        tree), so "records were dropped" is at least as urgent as
        "goals are closeable" — arguably more, since it can be the CAUSE
        of a wrong closeable_goals reading.
        """
        lint = work_reloader.current().lint()
        return json.dumps(work_status_payload(lint), indent=2, ensure_ascii=False)
