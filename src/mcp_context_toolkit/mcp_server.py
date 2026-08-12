from __future__ import annotations

import datetime
import importlib.util
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from mcp_context_toolkit import capability
from mcp_context_toolkit.core import (
    discover_work_dir,
    discover_shared_rules_dir,
    store_conventions,
    warn_if_example_rules,
)
from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.usage import UsageStore
from mcp_context_toolkit.work import WorkEngine


def _discover_rules_dir() -> Path | None:
    """Locate the PROJECT rules tier. Env CONTEXT_RULES_DIR wins, else walk up
    for <conv>/rules. Returns None if none found — the server then runs with
    the shared tier only (or no rules at all): a workspace without own rules
    must NOT lose the memory tools or the shared grundregeln (same gating
    rationale as the optional memory tiers)."""
    env = os.environ.get("CONTEXT_RULES_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        return p if p.is_dir() else None

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in store_conventions():
            rules = candidate / conv / "rules"
            if rules.is_dir():
                return rules
    return None


def _discover_memory_dir() -> Path | None:
    """Locate the PROJECT memory directory. Env CONTEXT_MEMORY_DIR wins, else
    walk up for .context/memory (or .claude/memory). Returns None if none found —
    memory tools are then simply not registered and rules keep working untouched."""
    env = os.environ.get("CONTEXT_MEMORY_DIR")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            mem = candidate / conv / "memory"
            if mem.is_dir():
                return mem
    return None


def _discover_wissen() -> "capability.CapabilityQuelle | None":
    """Wire the capability index: project root, index file, builder, tokenizer.

    Returns None — and the tool is simply not registered — when any piece is
    missing. Deliberately fail-closed on the TOKENIZER in particular: index and
    query must split words identically, and a fallback tokenizer would answer
    plausibly while silently missing every term the index actually holds. A
    missing tool is diagnosable; a subtly wrong one is not.
    """
    env = os.environ.get("CONTEXT_INDEX_FILE")
    for candidate in [Path.cwd(), *Path.cwd().parents]:
        for conv in store_conventions():
            if not (candidate / conv).is_dir():
                continue
            index = Path(env) if env else candidate / conv / ".index" / "wissen.json"
            bauer = candidate / "scripts" / "wissens_index.py"
            if not bauer.is_file():
                continue
            spec = importlib.util.spec_from_file_location("wissens_index", bauer)
            if spec is None or spec.loader is None:
                continue
            try:
                modul = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(modul)
                zerlege = modul.zerlege
            except Exception:
                return None
            return capability.CapabilityQuelle(candidate, index, zerlege, bauer)
    return None


def _optional_dir(env_name: str) -> Path | None:
    """Resolve an optional tier directory from an env var. Returns None if the
    var is unset/empty or does not point at an existing directory. Used for the
    cross-project 'user' memory tier (CONTEXT_USER_MEMORY_DIR), the org 'core'
    memory tier (CONTEXT_CORE_MEMORY_DIR) and the shared org RULES tier
    (CONTEXT_SHARED_RULES_DIR) — all purely additive, absent by default."""
    raw = os.environ.get(env_name)
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


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

    @property
    def watch_dirs(self) -> list[Path]:
        """Watched dirs as a public, copy-stable view (read-only)."""
        return list(self._watch)

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
    rules_reloader: _Reloader,
    memory_reloader: _Reloader | None = None,
    usage: UsageStore | None = None,
    work_reloader: _Reloader | None = None,
    wissen: capability.CapabilityQuelle | None = None,
):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("context")

    # Tool inventory, recorded AS the tools register — so the startup banner can
    # state a measured count instead of a hand-maintained literal. A number that
    # has to be edited by hand drifts silently the moment a tool is added; this
    # toolkit's own line is that a system must measure its state, not claim it.
    inventory: dict[str, list[str]] = {"rules": [], "memory": [], "work": [],
                                   "capability": []}

    def _tool(group: str):
        """Register an MCP tool and record its name under `group`."""
        def decorator(fn):
            inventory[group].append(fn.__name__)
            return mcp.tool()(fn)
        return decorator

    mcp.context_toolkit_inventory = inventory  # type: ignore[attr-defined]

    @_tool("rules")
    def query_rules_for_file(file_path: str) -> str:
        """Return codebase intelligence for a file: Rules, Decisions, Dependencies
        AND the open work items someone recorded for exactly this file.

        Use this as the first action when opening a file in a session — it filters
        the repo knowledge down to what matters here.

        `offene_arbeit` are NOTES, not instructions: someone wrote them for this
        file and has not closed them. If your edit covers one, close it (with
        `evidence`); if not, leave it. `get_work(<key>)` shows the full text.
        Matched via `kanten` (forward, recorded before building) and
        `evidence.files` of still-open records; capped at 8, because a wall of
        items is skimmed and a handful is read.
        """
        engine = rules_reloader.current()
        matches = engine.query_for_file_tiered(file_path)
        rules_out = [_rule_summary(r) for r in matches]

        decisions = engine.query_decisions_for_file(file_path)
        decisions_out = [d.model_dump(mode="json") for d in decisions]

        deps = engine.query_dependencies(file_path)

        # Vierte Auskunft: was hat jemand fuer GENAU diese Datei notiert und noch
        # nicht erledigt. Aus "hier sind deine Regeln" wird "hier sind deine
        # Regeln und die N offenen Punkte dazu". Fail-soft: fehlt der Work-Store
        # oder bricht das Laden, entfaellt der Abschnitt still — eine Zugabe
        # darf einen Edit nie aufhalten.
        arbeit_out: list[dict] = []
        if work_reloader is not None:
            try:
                for w in work_reloader.current().fuer_datei(file_path):
                    arbeit_out.append({
                        "key": w.key, "title": w.title, "status": w.status,
                        "theme": w.theme, "kind": w.kind,
                        "kanten": w.kanten is not None,
                    })
            except Exception:
                arbeit_out = []

        out = {
            "rules": rules_out,
            "decisions": decisions_out,
            "dependencies": deps,
            "offene_arbeit": arbeit_out,
        }
        return json.dumps(out, indent=2)

    @_tool("rules")
    def get_rule(key: str) -> str:
        """Fetch the full content of a single rule by key."""
        rule = rules_reloader.current().get_rule(key)
        if rule is None:
            return json.dumps({"error": f"rule not found: {key}"})
        return json.dumps(_rule_full(rule), indent=2, default=str)

    @_tool("rules")
    def list_rule_keys(type: str | None = None, scope: str | None = None) -> str:
        """List all loaded rule keys, optionally filtered by type and/or scope."""
        keys = rules_reloader.current().list_keys(type=type, scope=scope)  # type: ignore[arg-type]
        return json.dumps(keys, indent=2)

    @_tool("rules")
    def validate_rules() -> str:
        """Re-validate all rules from disk, across EVERY loaded tier. Returns
        {ok, rule_count, errors, warnings, roots}.

        Use this to check whether a recent YAML edit broke the rule set, whether
        any `conflicts_with` references point to non-existent rules, or whether a
        project rule now collides with a shared non_negotiable rule. Does NOT
        mutate the running engine's rule cache — it reads fresh from disk.
        """
        import json as _json

        from mcp_context_toolkit.engine import RuleLoadError

        engine = rules_reloader.current()
        roots = engine.roots
        if not roots:
            return _json.dumps({"ok": False, "error": "no rules loaded"})

        agg: dict[str, Any] = {
            "ok": True, "rule_count": 0, "errors": [], "warnings": [], "roots": [],
        }
        for root_path, tier in roots:
            res = RulesEngine.validate_directory(root_path)
            agg["rule_count"] += res["rule_count"]
            agg["errors"].extend(f"[{tier}] {e}" for e in res["errors"])
            agg["warnings"].extend(f"[{tier}] {w}" for w in res["warnings"])
            agg["roots"].append(
                {"tier": tier, "root": str(root_path),
                 "ok": res["ok"], "rule_count": res["rule_count"]}
            )
            if not res["ok"]:
                agg["ok"] = False

        # Cross-tier re-check: reload all tiers together to catch a project rule
        # that now shadows a shared non_negotiable one (raised only on the
        # combined load, invisible to per-directory validation).
        try:
            RulesEngine.from_roots({tier: rp for rp, tier in roots}, strict=False)
        except RuleLoadError as e:
            agg["ok"] = False
            agg["errors"].append(f"[cross-tier] {e}")

        return _json.dumps(agg, indent=2)

    @_tool("rules")
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

        @_tool("memory")
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

        @_tool("memory")
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

        @_tool("memory")
        def list_memories(type: str | None = None, tier: str | None = None) -> str:
            """List memory names + descriptions, optionally filtered by type
            (user/feedback/project/reference) and/or tier (user/project)."""
            ms = memory_reloader.current().list(type=type, tier=tier)  # type: ignore[arg-type]
            return json.dumps(
                [_memory_summary(m) for m in ms], indent=2, ensure_ascii=False
            )

        @_tool("memory")
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

        @_tool("memory")
        def memory_lint() -> str:
            """Hygiene report over the memory store — broken [[links]], index
            orphans, stale index pointers. The raw material a consolidation
            pass acts on."""
            return json.dumps(
                memory_reloader.current().lint(), indent=2, ensure_ascii=False
            )

        @_tool("memory")
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

    # ---- work-item tools (registered only when a work store was found) ----
    if work_reloader is not None:

        def _work_summary(w: Any) -> dict:
            # `kanten` als bool, nicht als Inhalt: der plan-keeper muss vor jedem
            # Scharfschalten wissen OB eine Reuse-Suche aufgezeichnet ist. Ohne
            # dieses Feld musste er jeden Record einzeln per get_work holen —
            # eine Pruefung, die teuer ist, wird uebersprungen.
            return {"key": w.key, "title": w.title, "kind": w.kind,
                    "status": w.status, "theme": w.theme, "part_of": w.part_of,
                    "kanten": w.kanten is not None}

        @_tool("work")
        def work_recall(query: str, limit: int = 8) -> str:
            """Find work items (plans/roadmaps as records) by keyword, ranked
            BM25F over title + body — same ranking algorithm as memory's
            recall(), via the shared core.bm25_scores. No frecency/backlink
            boost (unlike memory): nothing in this store's design asked for a
            'hot work item' concept (W20 — ask before inventing).

            Use this to find an existing item before creating a new one — the
            store's whole purpose is one record per idea, not a duplicate
            because the existing one wasn't found."""
            engine = work_reloader.current()
            hits = engine.recall(query, limit=limit)
            return json.dumps(
                [_work_summary(w) for w in hits], indent=2, ensure_ascii=False
            )

        @_tool("work")
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

        @_tool("work")
        def list_work(
            theme: str | None = None,
            status: str | None = None,
            kind: str | None = None,
        ) -> str:
            """List work items, optionally filtered by theme/status/kind.
            Each row includes an x/y rollup when the item has children —
            computed on the fly, never a maintained field."""
            engine = work_reloader.current()
            items = engine.list(theme=theme, status=status, kind=kind)
            out = []
            for w in items:
                row = _work_summary(w)
                roll = engine.rollup(w.key)
                if roll["total"]:
                    row["rollup"] = roll
                out.append(row)
            return json.dumps(out, indent=2, ensure_ascii=False)

        @_tool("work")
        def work_children(key: str) -> str:
            """Direct children of a goal via `part_of` — the hierarchy IS this
            relation, not a fixed level. A leaf work item (the median case on
            a real backlog) simply has none."""
            engine = work_reloader.current()
            return json.dumps(
                [_work_summary(w) for w in engine.children(key)],
                indent=2, ensure_ascii=False,
            )

        @_tool("work")
        def work_lint() -> str:
            """Hygiene report over the work store: dangling part_of/amends/
            supersedes/blocked_by references, goals whose children are ALL
            closed but the goal itself is not (the exact "recommended closing
            it, nobody did" failure mode this store was built to catch),
            done items with no evidence, and half-closed status/date pairs."""
            return json.dumps(
                work_reloader.current().lint(), indent=2, ensure_ascii=False
            )

        @_tool("work")
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
            closeable = len(lint["closeable_goals"])
            dangling = len(lint["dangling_refs"])
            no_evidence = len(lint["missing_evidence"])
            half_closed = len(lint["unclosed_pairs"])
            dropped = len(lint["load_errors"])
            # Die zwei juengeren Pruefungen MUSSEN mitzaehlen. Sonst stand
            # `reuse_ungeprueft: 130` neben der Empfehlung "kleine Reste" —
            # genau die Zahl, fuer die der Store umgebaut wurde, fehlte in
            # seinem eigenen Statusbericht (Reviewer-Fund 2026-08-04).
            plan_tot = len(lint.get("plan_path_broken", {}))
            ungeprueft = len(lint.get("reuse_ungeprueft", []))
            total_issues = (closeable + dangling + no_evidence + half_closed
                            + dropped + plan_tot + ungeprueft)
            if dropped:
                recommendation = "🚨 Records wurden beim Laden verworfen — Zahlen oben ggf. unvollstaendig"
            elif closeable or dangling or plan_tot:
                recommendation = "🧹 Aufraeumen faellig"
            elif ungeprueft:
                recommendation = (f"🔎 {ungeprueft} Records arbeiten ohne aufgezeichnete "
                                  f"Reuse-Suche (`kanten:` fehlt)")
            elif total_issues:
                recommendation = "💡 kleine Reste"
            else:
                recommendation = "✅ kein dringender Bedarf"
            return json.dumps({
                "total_items": lint["total"],
                "closeable_goals": closeable,
                "dangling_refs": dangling,
                "missing_evidence": no_evidence,
                "unclosed_pairs": half_closed,
                "plan_path_broken": plan_tot,
                "reuse_ungeprueft": ungeprueft,
                "load_errors": dropped,
                "recommendation": recommendation,
            }, indent=2, ensure_ascii=False)

    # ---- capability search (registered only when an index source was wired) ----
    if wissen is not None:

        @_tool("capability")
        def find_capability(frage: str, limit: int = 5,
                            art: str | None = None) -> str:
            """Search what this codebase ALREADY does, before writing anything new.

            Call this in the PLAN moment — before the first line of a new
            function, class or module. It answers one question: does this
            capability already exist here?

            **Use the technical terms — the query language does not matter.**
            The index holds identifiers and docstrings. What decides a hit is
            whether the query carries the domain vocabulary (`WORM`, `audit`,
            `PII`, `upload`, `chunk`, `embed`), not whether the sentence around
            them is German or English. Measured 2026-08-04, same target
            `audit_manager.log_action`:

                "jede aendernde Operation revisionssicher protokollieren"   miss
                "WORM-Audit-Log fuer eine aendernde Operation schreiben"    rank 3
                "log action to WORM audit trail"                           rank 1

            German prose with English technical terms is how this project
            actually speaks, and it works. What fails is paraphrasing the
            vocabulary away. If a query comes back weak, do not translate it —
            put the domain words back in.

            Args:
                frage: what the new code is supposed to DO, in code vocabulary.
                limit: how many candidates to return (default 5; three to five
                    are a decision, thirty are a wall of text).
                art: restrict to one kind — "funktion", "baustein" or "plan".
                    A "plan" hit means another plan already intends to build
                    this; talk to it instead of building beside it.

            Returns a ranked list. Each candidate names WHY it surfaced (a term
            match, or proximity to one along the call/import graph) and carries
            one sentence of description with its source, so the decision needs
            no file to be opened. The result is a SUGGESTION — whether a
            candidate fits is a judgement about meaning, and that judgement is
            yours.
            """
            index = wissen.current()
            if index is None:
                return json.dumps({
                    "fehler": "kein Wissens-Index vorhanden",
                    "hinweis": ("erzeugen mit: python3 scripts/wissens_index.py "
                                "(er entsteht sonst automatisch nach jedem Commit)"),
                })
            ergebnis = index.suche(frage, limit=limit, art=art)
            return capability.formatiere(
                ergebnis, capability.drift(index.stand, wissen.projekt))

    return mcp


# The "big sign" shown on server start. Attribution is deliberate — this is a
# named tool; forks are free to change AUTHOR.
TOOLKIT_NAME = "mcp-context-toolkit"
AUTHOR = "Othmar Atzmüller"
AUTHOR_URL = "github.com/othmaratzmueller-bit"


def _print_banner(
    engine: RulesEngine,
    memory_engine: MemoryEngine | None,
    inventory: dict[str, list[str]] | None = None,
    work_engine: WorkEngine | None = None,
) -> None:
    """Print the startup banner to stderr — every MCP client that launches the
    server sees it (generic, not tied to any one client). Lists what is loaded
    and credits the author.

    ``inventory`` is the {group: [tool names]} map recorded while the tools
    registered (see build_server). Passing it makes the Tools line a MEASURED
    figure; without it the line is omitted rather than guessed — this toolkit
    does not claim a state it has not counted."""
    from collections import Counter

    rule_types = Counter(r.type for r in engine.rules)
    rule_breakdown = " · ".join(
        f"{n} {t}" for t, n in sorted(rule_types.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    # If a shared org tier is loaded alongside project, name the tiers so the
    # operator sees the grundregeln are active (mirrors the Memory tier display).
    rule_tiers = Counter(r.tier for r in engine.rules)
    if len(rule_tiers) > 1:
        tier_label = " + ".join(
            t for t, _ in sorted(rule_tiers.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        rule_detail = f"{tier_label} · {rule_breakdown}" if rule_breakdown else tier_label
    else:
        rule_detail = rule_breakdown or "—"
    if engine.load_errors:
        rule_detail = f"{rule_detail} · {len(engine.load_errors)} skipped (invalid)"
    data = [("Rules", len(engine.rules), rule_detail)]
    if memory_engine is not None:
        tiers = Counter(m.tier for m in memory_engine.memories)
        tier_breakdown = " + ".join(
            t for t, _ in sorted(tiers.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        data.append((
            "Memory", len(memory_engine.memories),
            f"{tier_breakdown} · frecency-ranked" if tier_breakdown else "—",
        ))
    else:
        data.append(("Memory", 0, "(no store found)"))
    if work_engine is not None:
        statuses = Counter(w.status for w in work_engine.items)
        status_breakdown = " · ".join(
            f"{n} {s}" for s, n in sorted(statuses.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        work_detail = status_breakdown or "—"
        if work_engine.errors:
            work_detail = f"{work_detail} · {len(work_engine.errors)} skipped (invalid)"
        data.append(("Work", len(work_engine.items), work_detail))
    else:
        data.append(("Work", 0, "(no store found)"))
    if inventory is not None:
        groups = [(g, len(names)) for g, names in inventory.items() if names]
        data.append((
            "Tools",
            sum(n for _, n in groups),
            " · ".join(f"{n} {g}" for g, n in groups) or "—",
        ))

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
    # PROJECT rules tier is OPTIONAL (since the coder productization): a
    # workspace without own rules must not kill the server — memory tools and
    # the shared grundregeln keep working (same gating rationale as B1 memory).
    rules_dir = _discover_rules_dir()

    if rules_dir is not None:
        warn_if_example_rules(rules_dir)

    # Optional SHARED (org) rules tier — the grundregeln every project inherits
    # (code-is-law, no-fly-bys, ask-on-drift …). Env-only; absent → single-tier,
    # byte-identical to before. Loaded AFTER the project tier so project wins on a
    # (non-security) key collision; a non_negotiable collision fails loud.
    shared_rules_dir = discover_shared_rules_dir()

    if rules_dir is None and shared_rules_dir is None:
        print(
            "[context-toolkit] no rules found (neither project nor shared tier) — "
            "serving with an empty rule set.",
            file=sys.stderr,
        )

    # Wrap the rules engine in a reloader so an in-session YAML edit (or a
    # parallel session's) is picked up at the next tool call, not just at restart.
    watch_dirs: list = []
    if rules_dir is not None:
        watch_dirs.append(rules_dir)
        decisions_dir = rules_dir.parent / "decisions"
        if decisions_dir.is_dir():
            watch_dirs.append(decisions_dir)
    if shared_rules_dir is not None:
        watch_dirs.append(shared_rules_dir)

    def _load_rules() -> RulesEngine:
        # strict=False on the SERVING path: one schema-invalid project YAML must
        # not blank the entire rule set (incl. the shared grundregeln from a
        # different tier). CI/pytest/validate_rules stay strict. Skipped files
        # are surfaced below via engine.load_errors — degrade loud, not silent.
        roots: dict[str, Path | str] = {}
        if rules_dir is not None:
            roots["project"] = rules_dir
        if shared_rules_dir is not None:
            roots["shared"] = shared_rules_dir
        return RulesEngine.from_roots(roots, strict=False) if roots else RulesEngine()

    rules_reloader = _Reloader(_load_rules, watch_dirs)
    engine = rules_reloader.current()  # initial build (no reload yet)

    # Degrade loud (W4): if the lenient load skipped any file, name each one on
    # stderr so a broken project YAML is diagnosable, not a silent gap.
    if engine.load_errors:
        # "file(s)", not "rule file(s)": since v1.4.492 this channel also carries
        # decision-store failures (prefix `[decision]`). Saying "rule" made the
        # banner report three broken decisions as "3 rule file(s) skipped" while
        # all 59 rules had loaded — a count pointing at the wrong category.
        print(
            f"[context-toolkit] WARNING: {len(engine.load_errors)} file(s) "
            f"skipped (invalid) — the rest loaded normally:",
            file=sys.stderr,
        )
        for err in engine.load_errors:
            print(f"[context-toolkit]   - {err}", file=sys.stderr)

    # Auto-write fallback markdown so there's always a plain-text reference
    # for the MCP-outage case. Silent on failure — fallback is nice-to-have.
    # Only with a project tier: the fallback lives inside the project store.
    if rules_dir is not None:
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
    mem_dir = _discover_memory_dir()                       # project tier (optional)
    user_mem_dir = _optional_dir("CONTEXT_USER_MEMORY_DIR")  # user tier, cross-project (optional)
    core_mem_dir = _optional_dir("CONTEXT_CORE_MEMORY_DIR")  # core tier, org/institutional (optional)
    # Register the memory tools if ANY tier is present — NOT only when a project
    # tier exists. A fresh workspace (no .context/memory) must still expose the
    # user/core tiers, otherwise a brand-new project silently loses all memory tools.
    if mem_dir is not None or user_mem_dir is not None or core_mem_dir is not None:

        def _load_memory() -> MemoryEngine:
            me = MemoryEngine()
            # Load order IS precedence: project > user > core — the earlier-loaded
            # tier wins on a name collision (specific project note beats the shared
            # org reference; the core tier never overrides a personal/project note).
            if mem_dir is not None:
                me.load_directory(mem_dir, tier="project", strict=False)
            if user_mem_dir is not None:
                me.load_directory(user_mem_dir, tier="user", strict=False)
            if core_mem_dir is not None:
                me.load_directory(core_mem_dir, tier="core", strict=False)
            return me

        memory_reloader = _Reloader(_load_memory, [mem_dir, user_mem_dir, core_mem_dir])
        # Hot/cold (frecency) signal — sidecar lives in the PROJECT memory dir; only
        # meaningful when a project tier is present.
        if mem_dir is not None:
            usage = UsageStore.for_memory_dir(mem_dir)

    memory_engine = memory_reloader.current() if memory_reloader is not None else None

    # Work-item store is optional, same gating shape as memory: if the dir is
    # not there, work tools are simply not registered and everything else
    # keeps working untouched.
    work_reloader = None
    work_dir = discover_work_dir()
    if work_dir is not None:
        def _load_work() -> WorkEngine:
            return WorkEngine.from_directory(work_dir, strict=False)

        work_reloader = _Reloader(_load_work, [work_dir])

    work_engine = work_reloader.current() if work_reloader is not None else None
    # Degrade loud (W4), same as the rules-loading path above: a dropped work
    # record is not cosmetic — lint()'s closeable_goals counts a goal's
    # CURRENT children, so a goal whose only still-open child failed to load
    # looks fully closeable. Naming each skipped file on stderr makes the gap
    # diagnosable at start, on top of work_lint/work_status surfacing it live.
    if work_engine is not None and work_engine.errors:
        print(
            f"[context-toolkit] WARNING: {len(work_engine.errors)} work item file(s) "
            f"skipped (invalid) — the rest loaded normally:",
            file=sys.stderr,
        )
        for err in work_engine.errors:
            print(f"[context-toolkit]   - {err}", file=sys.stderr)

    # Build BEFORE printing: the banner reports the tool count recorded during
    # registration, so the figure can never drift from what is actually served.
    wissen = _discover_wissen()
    server = build_server(rules_reloader, memory_reloader, usage,
                          work_reloader, wissen)
    _print_banner(
        engine, memory_engine,
        getattr(server, "context_toolkit_inventory", None), work_engine,
    )
    server.run()


if __name__ == "__main__":
    main()
