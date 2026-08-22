"""Custom-domain MCP tools (`.context/domains/<id>/domain.yaml`).
A user-configured domain is either
its OWN physical store (`root` set, `source` empty) or a named VIEW that
filters an EXISTING store this server already loaded (`source` set to the
profile it views). A view never re-reads the store from disk — it queries
the SAME reloader's `.current()` the matching plain tool (query_rules/
recall/work_recall/docs_recall) already uses, so a domain view can never
drift from what those tools report."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_context_toolkit.core import bm25_scores, build_bm25_index, clamp_limit
from mcp_context_toolkit.docs import DocsEngine
from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.work import WorkEngine

from .serializers import _doc_section_summary, _memory_summary


def _domain_root(m) -> Path | None:
    """A physical-store domain's `root:` is relative to its OWN
    domain.yaml directory — a self-contained unit, movable without
    recomputing paths against the workspace root.

    Bounded to that same directory: `root` is data a domain author
    wrote and, unlike the rules/memory/work/docs stores (which are
    reached only through fixed discovery, never a value inside a
    file), this is the first field where a manifest can point the
    reader anywhere on disk. `discover_domains_dir()` walks UP from
    cwd, so opening a cloned repo that ships its own
    `.context/domains/` auto-activates its manifests — a `root:
    ../../../../etc` in someone else's file must not resolve outside
    its own domain directory. Returns `None` for both "no root
    configured" and "root escapes" — same caller-visible outcome,
    since either way there is nothing safe to read.
    """
    if not m.root or not m.source_path:
        return None
    own_dir = Path(m.source_path).resolve().parent
    resolved = (own_dir / m.root).resolve()
    try:
        resolved.relative_to(own_dir)
    except ValueError:
        return None
    return resolved


def register(mcp_tool, domain_reloader, rules_reloader=None,
            work_reloader=None, memory_reloader=None, docs_reloader=None) -> None:
    """Register the domain tools. The four `*_reloader` args are the SAME
    reloaders the plain rules/work/memory/docs tools use — a view domain
    queries them directly, a physical domain loads fresh from its own
    `root`."""

    @mcp_tool("domain")
    def list_domains() -> str:
        """List configured custom domains — id, title, profile, whether
        it's a physical store or a named view over an existing one, and
        its routing keywords/paths (`.context/domains/<id>/domain.yaml`,
        see README's "Domains" section)."""
        return json.dumps(domain_reloader.current().list_domains(),
                          indent=2, ensure_ascii=False)

    @mcp_tool("domain")
    def get_domain(domain_id: str) -> str:
        """Full manifest for one configured domain — including its
        filter and attributes. `{"error": ...}` if the id is unknown."""
        m = domain_reloader.current().get_domain(domain_id)
        if m is None:
            return json.dumps({"error": f"unknown domain: {domain_id}"}, ensure_ascii=False)
        return json.dumps(m.model_dump(mode="json"), indent=2, ensure_ascii=False)

    @mcp_tool("domain")
    def query_domain(domain_id: str, query: str, limit: int = 8) -> str:
        """Keyword-search one configured domain. Dispatches to the store
        its `profile` names (rules/memory/work/docs); a named VIEW
        additionally applies its declared `filter` first (rules:
        types/priorities — work: themes/statuses) before ranking the
        filtered candidates with the same BM25F scoring the plain
        recall tools use. `{"error": ...}` for an unknown domain, an
        unloaded view target, or a `rules` profile queried without any
        filter narrowing it (rules have no free-text body worth ranking
        on their own — narrow with `types`/`priorities` first).

        `limit` is clamped via `core.clamp_limit` — the rules/work branches
        below build their own local BM25 index and slice manually (domain
        filtering has to happen BEFORE ranking), so they don't inherit the
        clamp memory.recall()/docs.recall() apply internally; clamping once
        here, before any branch, covers all four."""
        m = domain_reloader.current().get_domain(domain_id)
        if m is None:
            return json.dumps({"error": f"unknown domain: {domain_id}"}, ensure_ascii=False)
        limit = clamp_limit(limit)
        profile = m.source or m.profile
        is_view = m.source is not None

        if profile == "rules":
            if not m.filter.types and not m.filter.priorities:
                return json.dumps(
                    {"error": f"domain '{domain_id}': a rules profile has no free-text "
                              "body worth ranking on its own — narrow it with "
                              "filter.types/filter.priorities first"},
                    ensure_ascii=False)
            if is_view:
                if rules_reloader is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' views 'rules', but no rules store is loaded"},
                        ensure_ascii=False)
                candidates = rules_reloader.current().query()
            else:
                root = _domain_root(m)
                if root is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' has no usable root and is not a view"},
                        ensure_ascii=False)
                candidates = RulesEngine.from_directory(root).query()
            if m.filter.types:
                candidates = [r for r in candidates if r.type in m.filter.types]
            if m.filter.priorities:
                candidates = [r for r in candidates if r.priority in m.filter.priorities]
            index = build_bm25_index({
                r.key: [(r.title, 3.0), (r.summary, 2.0), (r.content, 1.0)]
                for r in candidates
            })
            scores = bm25_scores(index, query)
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
            by_key = {r.key: r for r in candidates}
            return json.dumps([
                {"key": k, "title": by_key[k].title, "type": by_key[k].type,
                 "priority": by_key[k].priority}
                for k, _ in ranked
            ], indent=2, ensure_ascii=False)

        if profile == "work":
            if is_view:
                if work_reloader is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' views 'work', but no work store is loaded"},
                        ensure_ascii=False)
                items = work_reloader.current().list(include_archived=True)
            else:
                root = _domain_root(m)
                if root is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' has no usable root and is not a view"},
                        ensure_ascii=False)
                items = WorkEngine.from_directory(root, strict=False).list(include_archived=True)
            if m.filter.themes:
                items = [i for i in items if i.theme in m.filter.themes]
            if m.filter.statuses:
                items = [i for i in items if i.status in m.filter.statuses]
            index = build_bm25_index({i.key: [(i.title, 3.0), (i.body, 1.0)] for i in items})
            scores = bm25_scores(index, query)
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
            by_key = {i.key: i for i in items}
            return json.dumps([
                {"key": k, "title": by_key[k].title, "status": by_key[k].status,
                 "theme": by_key[k].theme}
                for k, _ in ranked
            ], indent=2, ensure_ascii=False)

        if profile == "memory":
            if is_view:
                if memory_reloader is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' views 'memory', but no memory store is loaded"},
                        ensure_ascii=False)
                me = memory_reloader.current()
            else:
                root = _domain_root(m)
                if root is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' has no usable root and is not a view"},
                        ensure_ascii=False)
                me = MemoryEngine()
                me.load_directory(root, tier="project", strict=False)
            hits = me.recall(query, limit=limit)
            return json.dumps([_memory_summary(h) for h in hits], indent=2, ensure_ascii=False)

        if profile == "docs":
            if is_view:
                if docs_reloader is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' views 'docs', but no docs store is loaded"},
                        ensure_ascii=False)
                de = docs_reloader.current()
            else:
                root = _domain_root(m)
                if root is None:
                    return json.dumps(
                        {"error": f"domain '{domain_id}' has no usable root and is not a view"},
                        ensure_ascii=False)
                de = DocsEngine.from_directory(root)
            hits = de.recall(query, limit=limit)
            return json.dumps([_doc_section_summary(s) for s in hits], indent=2, ensure_ascii=False)

        return json.dumps({"error": f"unsupported profile: {profile}"}, ensure_ascii=False)
