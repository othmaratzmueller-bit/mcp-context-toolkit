"""Server wiring: discover stores, build reloaders, register each domain's
tools, print the startup banner, run. `build_server()`/`main()` are the two
entry points — everything else in this module is a private helper for them."""

from __future__ import annotations

import sys
from pathlib import Path

from mcp_context_toolkit import capability
from mcp_context_toolkit.core import (
    discover_domains_dir,
    discover_shared_rules_dir,
    discover_work_dir,
    warn_if_example_rules,
)
from mcp_context_toolkit.docs import DocsEngine
from mcp_context_toolkit.domain import DomainEngine
from mcp_context_toolkit.engine import RulesEngine
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.usage import UsageStore
from mcp_context_toolkit.work import WorkEngine

from . import (
    tools_baustein,
    tools_capability,
    tools_docs,
    tools_domains,
    tools_memory,
    tools_rules,
    tools_work,
)
from .discovery import (
    _discover_agent_memory_dir,
    _discover_docs_dir,
    _discover_memory_dir,
    _discover_rules_dir,
    _discover_wissen,
    _optional_dir,
    _optional_file,
)
from .reloaders import _Reloader

# The "big sign" shown on server start. Attribution is deliberate — this is a
# named tool; forks are free to change AUTHOR.
TOOLKIT_NAME = "mcp-context-toolkit"
AUTHOR = "Othmar Atzmüller"
AUTHOR_URL = "github.com/othmaratzmueller-bit"


def build_server(
    rules_reloader: _Reloader,
    memory_reloader: _Reloader | None = None,
    usage: UsageStore | None = None,
    work_reloader: _Reloader | None = None,
    wissen: capability.CapabilityQuelle | None = None,
    work_dir: Path | None = None,
    docs_reloader: _Reloader | None = None,
    baustein_docs_file: Path | None = None,
    function_docs_file: Path | None = None,
    domain_reloader: _Reloader | None = None,
):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("context")

    # Tool inventory, recorded AS the tools register — so the startup banner can
    # state a measured count instead of a hand-maintained literal. A number that
    # has to be edited by hand drifts silently the moment a tool is added; this
    # toolkit's own line is that a system must measure its state, not claim it.
    inventory: dict[str, list[str]] = {"rules": [], "memory": [], "work": [],
                                   "capability": [], "docs": [], "baustein": [],
                                   "domain": []}

    def _tool(group: str):
        """Register an MCP tool and record its name under `group`."""
        def decorator(fn):
            inventory[group].append(fn.__name__)
            return mcp.tool()(fn)
        return decorator

    mcp.context_toolkit_inventory = inventory  # type: ignore[attr-defined]

    tools_rules.register(_tool, rules_reloader, work_reloader)

    if memory_reloader is not None:
        tools_memory.register(_tool, memory_reloader, usage)

    if work_reloader is not None:
        tools_work.register(_tool, work_reloader, work_dir)

    if docs_reloader is not None:
        tools_docs.register(_tool, docs_reloader)

    if domain_reloader is not None:
        tools_domains.register(_tool, domain_reloader, rules_reloader,
                               work_reloader, memory_reloader, docs_reloader)

    if baustein_docs_file is not None or function_docs_file is not None:
        tools_baustein.register(_tool, baustein_docs_file, function_docs_file)

    if wissen is not None:
        tools_capability.register(_tool, wissen)

    return mcp


def _print_banner(
    engine: RulesEngine,
    memory_engine: MemoryEngine | None,
    inventory: dict[str, list[str]] | None = None,
    work_engine: WorkEngine | None = None,
    docs_engine: DocsEngine | None = None,
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
    if docs_engine is not None:
        files = len({s.path for s in docs_engine.sections})
        data.append((
            "Docs", len(docs_engine.sections),
            f"{files} file(s) · read-only" if files else "—",
        ))
    else:
        data.append(("Docs", 0, "(no store found)"))
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
        # Etappe C (2026-08-19): decisions folded into the Work-Store
        # (mcp_decisions_in_work_falten) — watch the new leaf.
        decisions_dir = rules_dir.parent / "work" / "decisions"
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
    agent_mem_dir = _discover_agent_memory_dir()           # agent-memory tree (optional)
    user_mem_dir = _optional_dir("CONTEXT_USER_MEMORY_DIR")  # user tier, cross-project (optional)
    core_mem_dir = _optional_dir("CONTEXT_CORE_MEMORY_DIR")  # core tier, org/institutional (optional)
    # Register the memory tools if ANY tier is present — NOT only when a project
    # tier exists. A fresh workspace (no .context/memory) must still expose the
    # user/core tiers, otherwise a brand-new project silently loses all memory tools.
    if mem_dir is not None or agent_mem_dir is not None or user_mem_dir is not None or core_mem_dir is not None:

        def _load_memory() -> MemoryEngine:
            me = MemoryEngine()
            # Load order IS precedence: project > user > core — the earlier-loaded
            # tier wins on a name collision (specific project note beats the shared
            # org reference; the core tier never overrides a personal/project note).
            if mem_dir is not None:
                me.load_directory(mem_dir, tier="project", strict=False)
            if agent_mem_dir is not None:
                me.load_directory(agent_mem_dir, tier="project", strict=False)
            if user_mem_dir is not None:
                me.load_directory(user_mem_dir, tier="user", strict=False)
            if core_mem_dir is not None:
                me.load_directory(core_mem_dir, tier="core", strict=False)
            return me

        memory_reloader = _Reloader(_load_memory, [mem_dir, agent_mem_dir, user_mem_dir, core_mem_dir])
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

    # Docs tree is optional, same gating shape as memory/work — prose docs
    # stay documents (Work-Record doku_lesend_indexieren_statt_zerreissen),
    # just made searchable via docs_recall.
    docs_reloader = None
    docs_dir = _discover_docs_dir()
    if docs_dir is not None:
        def _load_docs() -> DocsEngine:
            return DocsEngine.from_directory(docs_dir)

        docs_reloader = _Reloader(_load_docs, [docs_dir])

    docs_engine = docs_reloader.current() if docs_reloader is not None else None

    # Custom domains are optional, same gating shape as work/docs.
    domain_reloader = None
    domains_dir = discover_domains_dir()
    if domains_dir is not None:
        def _load_domains() -> DomainEngine:
            return DomainEngine.from_directory(domains_dir)

        domain_reloader = _Reloader(_load_domains, [domains_dir])

    # Baustein/function docs are two INDEPENDENTLY optional JSON files —
    # config-driven since they live at a TALOS-specific path this generic
    # engine never hard-codes (Work-Record `mcp_baustein_funktions_doku_
    # domaene_konvergiertes_design`).
    baustein_docs_file = _optional_file("CONTEXT_BAUSTEIN_DOCS_FILE")
    function_docs_file = _optional_file("CONTEXT_FUNCTION_DOCS_FILE")

    # Build BEFORE printing: the banner reports the tool count recorded during
    # registration, so the figure can never drift from what is actually served.
    wissen = _discover_wissen()
    server = build_server(rules_reloader, memory_reloader, usage,
                          work_reloader, wissen, work_dir, docs_reloader,
                          baustein_docs_file, function_docs_file, domain_reloader)
    _print_banner(
        engine, memory_engine,
        getattr(server, "context_toolkit_inventory", None), work_engine,
        docs_engine,
    )
    server.run()
