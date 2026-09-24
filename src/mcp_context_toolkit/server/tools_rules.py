"""Rules & Decisions MCP tools."""

from __future__ import annotations

import json
from typing import Any

from mcp_context_toolkit.engine import rules_for_file_payload

from .serializers import _rule_full, _rule_summary


def register(mcp_tool, rules_reloader, work_reloader=None) -> None:
    """Register the rules-domain tools. `work_reloader` is optional and only
    used by `query_rules_for_file` to attach the open work items recorded
    for the file — a cross-domain reach that predates the domain split and
    stays, rather than forcing a second round-trip on every caller."""

    @mcp_tool("rules")
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
        # Fail-soft (W8-Reviewer-Fund ERROR-1): `.current()` re-loads on a
        # changed mtime and can raise (deleted dir, permission error) — a
        # broken work store must never take the rules/decisions/dependencies
        # answer down with it, same contract `rules_for_file_payload`'s own
        # internal `for_file()` try/except already promises.
        work_engine = None
        if work_reloader is not None:
            try:
                work_engine = work_reloader.current()
            except Exception:
                work_engine = None
        out = rules_for_file_payload(engine, file_path, work_engine=work_engine)
        return json.dumps(out, indent=2)

    @mcp_tool("rules")
    def get_rule(key: str) -> str:
        """Fetch the full content of a single rule by key."""
        rule = rules_reloader.current().get_rule(key)
        if rule is None:
            return json.dumps({"error": f"rule not found: {key}"})
        return json.dumps(_rule_full(rule), indent=2, default=str)

    @mcp_tool("rules")
    def list_rule_keys(type: str | None = None, scope: str | None = None) -> str:
        """List all loaded rule keys, optionally filtered by type and/or scope."""
        keys = rules_reloader.current().list_keys(type=type, scope=scope)  # type: ignore[arg-type]
        return json.dumps(keys, indent=2)

    @mcp_tool("rules")
    def validate_rules() -> str:
        """Re-validate all rules from disk, across EVERY loaded tier. Returns
        {ok, rule_count, errors, warnings, roots}.

        Use this to check whether a recent YAML edit broke the rule set, whether
        any `conflicts_with` references point to non-existent rules, or whether a
        project rule now collides with a shared non_negotiable rule. Does NOT
        mutate the running engine's rule cache — it reads fresh from disk.
        """
        import json as _json

        from mcp_context_toolkit.engine import RuleLoadError, RulesEngine

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

    @mcp_tool("rules")
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
