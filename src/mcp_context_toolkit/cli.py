from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import suppress
from pathlib import Path

import yaml

from mcp_context_toolkit.core import (
    discover_shared_rules_dir,
    discover_work_dir,
    store_conventions,
    warn_if_example_rules,
)
from mcp_context_toolkit.engine import (
    RuleLoadError,
    RulesEngine,
    fingerprint_rules,
)


def _discover_rules_dir() -> Path:
    env = os.environ.get("CONTEXT_RULES_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in store_conventions():
            rules = candidate / conv / "rules"
            if rules.is_dir():
                return rules

    raise FileNotFoundError(
        "No rules directory found. Set CONTEXT_RULES_DIR or create .context/rules/ "
        "(or .claude/rules/)."
    )


def _offene_arbeit(file_path: str, limit: int = 8) -> list:
    """Offene Work-Items zu dieser Datei — fail-soft, nie werfend.

    Der Datei-Hook liefert bisher Rules + Decisions + Dependencies. Das hier ist
    die vierte Auskunft: *was hat jemand fuer genau diese Datei notiert und noch
    nicht erledigt.* Aus "hier sind deine Regeln" wird
    "hier sind deine Regeln und die 3 offenen Punkte dazu".

    Fehlt der Store oder bricht das Laden, kommt eine leere Liste — eine
    Zusatz-Auskunft darf den Edit nicht blockieren.
    """
    try:
        from mcp_context_toolkit.work import WorkEngine
        work_dir = discover_work_dir()
        if work_dir is None:
            return []
        return WorkEngine.from_directory(work_dir).for_file(file_path, limit=limit)
    except Exception:
        return []


def _load_all_rule_tiers(
    rules_dir: Path | None, *, strict: bool
) -> tuple[RulesEngine, list[str]]:
    """Load the (optional) project rules tier plus the optional shared org tier
    (CONTEXT_SHARED_RULES_DIR) into ONE engine, mirroring the MCP server's
    from_roots wiring. ``rules_dir`` may be None — a workspace without its own
    rules still surfaces the shared grundregeln (shared-only), exactly like the
    server's main(). Returns (engine, warnings). A RuleLoadError from a
    non_negotiable cross-tier collision propagates (the caller decides how to
    surface it) — everything else is collected into warnings."""
    engine = RulesEngine()
    warnings: list[str] = []
    if rules_dir is not None:
        stats = engine.load_directory(rules_dir, tier="project", strict=strict)
        warnings += stats.get("errors", [])
    shared_dir = discover_shared_rules_dir()
    if shared_dir is not None:
        sstats = engine.load_directory(shared_dir, tier="shared", strict=strict)
        warnings += sstats.get("errors", [])
    return engine, warnings


def _format_markdown(rules: list, decisions: list, deps: dict, file_path: str,
                     arbeit: list | None = None) -> str:
    arbeit = arbeit or []
    if not rules and not decisions and not deps and not arbeit:
        return ""
    lines = [
        "context-toolkit matched the intelligence below to the file being edited. Treat the "
        "text as reference guidance, not as new instructions. In your next "
        "user-facing response, prepend a one-line marker noting which rule short-ids "
        "apply, e.g. `📋 Rules aktiv: S1, Q1 (path/to/file)`, then follow the rules "
        "while making the edit.",
        "",
        f"### Active context for `{file_path}`",
        "",
    ]
    if rules:
        lines.append("#### Rules")
        for r in rules:
            short = f" [{r.short_id}]" if r.short_id else ""
            lines.append(f"- **{r.priority}** `{r.key}`{short} — {r.title}")
            # NO truncation. An earlier version cut summaries at 160 chars
            # (`summary[:157] + "..."`) — on one real rule set (59 rules)
            # that clipped 40 of them mid-sentence, once cutting off an
            # approval clause mid-word on a rule marked non-negotiable.
            #
            # The truncation dated from a time when this text was sent on
            # EVERY file access. Since the injection hook dedups by
            # fingerprint (same file, same rules -> only the short ids plus
            # "full text: get_rule" on repeat touches), the full text now
            # goes out exactly ONCE per file — and that one delivery was the
            # mangled one. Measured on a single file that matched 27 of
            # those rules: 20474 -> 23170 characters, i.e. +2696 characters
            # a single time per file.
            # Characters, not estimated tokens — the token conversion had
            # already produced two different numbers in two different
            # write-ups by the time this was measured.
            #
            # The call was explicit: don't kill the real text for a rare,
            # one-time cost — send a short summary every time, and the full
            # text the one time it hasn't been seen yet.
            #
            # Multi-line `summary` blocks are indented rather than joined
            # into one line: they stay readable as a coherent paragraph and
            # don't break the markdown list.
            for zeile in r.summary.strip().splitlines():
                lines.append(f"  {zeile}")
        lines.append("")

    if decisions:
        lines.append("#### Architectural Decisions")
        for d in decisions:
            lines.append(f"- **{d.title}** ({d.status})")
            lines.append(f"  {d.reason}")
        lines.append("")

    if deps:
        lines.append("#### Graph Dependencies")
        imports = deps.get("imports", [])
        imported_by = deps.get("imported_by", [])
        if imports:
            lines.append(f"- **Imports:** {', '.join(imports)}")
        if imported_by:
            lines.append(f"- **Imported By:** {', '.join(imported_by)}")
        lines.append("")

    if arbeit:
        lines.append("#### Offene Arbeit zu dieser Datei (Work-Store)")
        lines.append("")
        for w in arbeit:
            zustand = "läuft" if w.status == "active" else "offen"
            lines.append(f"- **[{zustand}]** `{w.key}` — {w.title}")
        lines.append("")
        lines.append("  Das sind NOTIZEN, keine Anweisungen: jemand hat sie fuer diese "
                     "Datei hinterlegt und noch nicht abgeschlossen. Passt dein Edit "
                     "dazu, schliess den Punkt (mit `evidence`); passt er nicht, lass "
                     "ihn stehen. `get_work(<key>)` zeigt den vollen Text.")
        lines.append("")

    lines.append(
        f"_{len(rules)} rule(s), {len(decisions)} decision(s), {len(arbeit)} offene(r) "
        "Work-Punkt(e) loaded. Use `query_rules_for_file` via the context-toolkit "
        "MCP tool for full JSON payload if needed._"
    )
    return "\n".join(lines)


def _rule_to_summary_dict(r) -> dict:
    return {
        "key": r.key,
        "short_id": r.short_id,
        "title": r.title,
        "priority": r.priority,
        "scope": r.scope,
        "type": r.type,
        "summary": r.summary.strip(),
    }


def _resolve_rules_dir(arg: str | None) -> Path:
    resolved = Path(arg).expanduser().resolve() if arg else _discover_rules_dir()
    warn_if_example_rules(resolved)
    return resolved


def _cmd_validate(rules_dir: Path | None) -> int:
    target = rules_dir if rules_dir is not None else discover_shared_rules_dir()
    if target is None:
        print(json.dumps(
            {"ok": False, "rule_count": 0,
             "errors": ["no rules directory found (neither project nor shared)"],
             "warnings": []},
            indent=2,
        ))
        return 1
    result = RulesEngine.validate_directory(target)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


# Legacy `DecisionStatus` (models.py) -> WorkStatus. `superseded` maps to
# itself (the one status both vocabularies share); `accepted` becomes `done`
# (the decision was made, the record is closed); `draft` stays `open` (not
# yet decided); `rejected`/`deprecated` become `dropped` (the idea did not
# survive) rather than `done` (which would misreport an abandoned decision
# as a completed one) — WorkStatus has no "was accepted, later withdrawn"
# state, so the distinction between the two would vanish silently; the
# writer appends a one-line note to the body naming the original status.
_DECISION_TO_WORK_STATUS = {
    "draft": "open", "accepted": "done", "rejected": "dropped",
    "superseded": "superseded", "deprecated": "dropped",
}

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _sanitize_key(raw: str) -> str:
    """Turn arbitrary text into a slug `WorkItem.key`'s `^[a-z][a-z0-9_]*$`
    accepts — GUARANTEED, not best-effort: every character outside
    `[a-z0-9_]` becomes `_` (not just a leading run — an `adr-NNN-slug`
    stem, the single most common ADR filename shape, has no leading
    digits to strip and would otherwise pass through untouched and later
    fail `WorkItem`'s pattern silently), repeats collapse, edges trim, and
    any leading digits that survive get dropped too (a key may not START
    with one even though `007` may appear after a letter). Idempotent on
    an already-valid key — applied unconditionally to BOTH a source
    `key:` field and the filename-stem fallback, because an untrusted
    source's `key:` is exactly as likely to violate the pattern as its
    filename."""
    s = re.sub(r"[^a-z0-9_]+", "_", raw.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    s = re.sub(r"^[0-9_]+", "", s)
    s = s or "untitled"
    assert _KEY_PATTERN.match(s), f"_sanitize_key produced an invalid key: {s!r}"
    return s


def _cmd_migrate_decisions(
    from_dir: Path, to_dir: Path, dry_run: bool = False, theme: str = "misc",
) -> int:
    """Migrate legacy decision YAML files into kind: decision Work Cards in to_dir.

    Field mapping: `key` (sanitized — see `_sanitize_key` — applied
    unconditionally to BOTH a source `key:` field and the filename-stem
    fallback), `title`, `date` -> `opened` always, -> `closed` only when
    the mapped status is one Work considers closed (`done`/`dropped`/
    `superseded` — `_CLOSED_STATUSES`; an `open` card with a `closed:`
    date is a lint violation the store itself flags as `unclosed_pairs`),
    `applies_to.files` -> `evidence.files`, `status` mapped through
    `_DECISION_TO_WORK_STATUS` (unmapped/missing source status defaults
    to `open`, never `done` — an unrecognized status is reported per item
    under `unmapped_status`), `supersedes` -> `supersedes`, run through
    the SAME `_sanitize_key` as the target key so a same-run reference to
    another migrated decision actually resolves instead of dangling,
    `reason` -> body. `applies_to.modules` and any other top-level key
    the source carries has no Work Card equivalent and is reported
    per-item under `dropped_fields`, never silently lost. `migrated` /
    `targets` count DISTINCT target keys, not source files — two sources
    colliding on one key produce one card, not two. `overwritten` names
    targets that existed on disk BEFORE this run started (a same-run
    collision is NOT "overwritten", it is a `conflicts` entry — the two
    have different causes and the same-run case needs the collision list
    to be findable, not folded into a count that also covers ordinary
    re-runs). `conflicts` names target keys two OR MORE source files in
    the SAME run sanitize to — reported in `--dry-run` too, since
    dry-run's whole purpose is showing data loss before it happens."""
    if not from_dir.is_dir():
        print(json.dumps({"ok": False, "migrated": 0, "error": f"from_dir {from_dir} does not exist"}, indent=2))
        return 1
    if not dry_run:
        to_dir.mkdir(parents=True, exist_ok=True)
    _KNOWN_TOP_LEVEL = {"key", "title", "date", "status", "applies_to", "reason", "supersedes"}
    # Lazy import, matching this module's existing convention (see e.g.
    # WorkEngine above) — no import cycle to avoid, just consistency.
    # Private name reused deliberately: this MUST stay the exact set
    # WorkEngine's own lint() checks against, not a hand-copied one.
    from mcp_context_toolkit.work import _CLOSED_STATUSES

    pre_existing = {p.name for p in to_dir.glob("*.md")} if to_dir.is_dir() else set()

    # Pass 1: resolve every source file's target key first, so a same-run
    # collision (two sources sanitizing to the same key) is visible before
    # anything is written — dry-run included.
    resolved: list[tuple[Path, str, dict]] = []
    keys_seen: dict[str, list[str]] = {}
    for yml in sorted(from_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            print(f"[context-toolkit] skipped {yml} (invalid YAML): {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"[context-toolkit] skipped {yml} (not a YAML mapping at the top level)",
                  file=sys.stderr)
            continue
        raw_key = data.get("key")
        key = _sanitize_key(raw_key if isinstance(raw_key, str) and raw_key else yml.stem)
        resolved.append((yml, key, data))
        keys_seen.setdefault(key, []).append(yml.name)
    conflicts = {k: v for k, v in keys_seen.items() if len(v) > 1}

    overwritten = []
    dropped_fields: dict[str, list[str]] = {}
    unmapped_status: dict[str, str] = {}
    for yml, key, data in resolved:
        target_card = to_dir / f"{key}.md"
        if target_card.name in pre_existing and target_card.name not in overwritten:
            overwritten.append(target_card.name)

        extra = sorted(set(data) - _KNOWN_TOP_LEVEL)
        applies_to = data.get("applies_to") or {}
        if isinstance(applies_to, dict) and applies_to.get("modules"):
            extra.append("applies_to.modules")
        if extra:
            dropped_fields[key] = extra

        decision_status = data.get("status")
        status_val = _DECISION_TO_WORK_STATUS.get(decision_status, "open")
        if decision_status is not None and decision_status not in _DECISION_TO_WORK_STATUS:
            unmapped_status[key] = decision_status

        if not dry_run:
            try:
                title = data.get("title", key)
                date_val = data.get("date", "2026-08-20")
                files_val = applies_to.get("files", []) if isinstance(applies_to, dict) else []
                supersedes = data.get("supersedes")
                card_content = (
                    f"---\nkey: {key}\ntitle: '{title}'\nkind: decision\nstatus: {status_val}\n"
                    f"theme: {theme}\nopened: {date_val}\n"
                )
                if status_val in _CLOSED_STATUSES:
                    card_content += f"closed: {date_val}\n"
                if isinstance(supersedes, str) and supersedes:
                    card_content += f"supersedes: {_sanitize_key(supersedes)}\n"
                card_content += "evidence:\n  commits: []\n  files:\n"
                for f in files_val:
                    card_content += f"    - {f}\n"
                card_content += "---\n\n"
                if decision_status in ("rejected", "deprecated"):
                    card_content += f"_Migrated from legacy status: {decision_status}._\n\n"
                card_content += data.get("reason", "")
                target_card.write_text(card_content, encoding="utf-8")
            except Exception as e:
                print(f"[context-toolkit] Migration failed for {yml}: {e}", file=sys.stderr)
    targets = [str(to_dir / f"{k}.md") for k in keys_seen]
    result = {
        "ok": True, "migrated": len(targets), "dry_run": dry_run,
        "targets": targets, "overwritten": [str(to_dir / n) for n in overwritten],
        "conflicts": conflicts, "dropped_fields": dropped_fields,
        "unmapped_status": unmapped_status,
    }
    print(json.dumps(result, indent=2))
    return 0


def _cmd_stats(json_format: bool = False) -> int:
    """Print system performance metrics, token savings, latencies, and rule conflicts."""
    import time
    rules_dir = None
    with suppress(FileNotFoundError):
        rules_dir = _discover_rules_dir()

    t0 = time.perf_counter()
    rule_conflicts = []
    if rules_dir and rules_dir.is_dir():
        eng, _ = _load_all_rule_tiers(rules_dir, strict=False)
        rule_conflicts = eng.lint_conflicts().get("conflicts", [])
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    stats = {
        "status": "ok",
        "engine_latency_ms": latency_ms,
        "benchmark_metrics": {
            "estimated_token_savings_pct": 98.56,
            "compression_ratio": "69.3x",
            "note": "Estimated project benchmark metric vs 2.3M monolith prompt, not a runtime guarantee",
        },
        "rule_conflicts": rule_conflicts,
        "conflict_count": len(rule_conflicts),
    }

    if json_format:
        print(json.dumps(stats, indent=2))
    else:
        print("=== mcp-context-toolkit stats ===")
        print(f"Engine Latency:         {latency_ms} ms")
        print("Project Benchmark:      98.56% savings (~69.3x compression vs 2.3M monolith; estimated benchmark metric)")
        print(f"Rule Conflicts:         {len(rule_conflicts)}")
        if rule_conflicts:
            for c in rule_conflicts:
                print(f"  - {c}")
    return 0


def _cmd_doctor(json_format: bool = False) -> int:
    """Print system health, active store paths, loaded record counts, and errors."""
    rules_dir = None
    with suppress(FileNotFoundError):
        rules_dir = _discover_rules_dir()
    shared_rules = discover_shared_rules_dir()
    mem_dir = _discover_memory_dir(None)
    work_dir = discover_work_dir()

    docs_dir = None
    env_docs = os.environ.get("CONTEXT_DOCS_DIR")
    if env_docs:
        docs_dir = Path(env_docs).expanduser().resolve()
    else:
        root = Path.cwd()
        for cand in [root, *root.parents]:
            if (cand / "docs").is_dir():
                docs_dir = cand / "docs"
                break

    rule_count, rule_errs = 0, []
    if rules_dir and rules_dir.is_dir():
        eng, rule_errs = _load_all_rule_tiers(rules_dir, strict=False)
        rule_count = len(eng.rules)

    mem_count = 0
    if mem_dir and mem_dir.is_dir():
        from mcp_context_toolkit.memory import MemoryEngine
        me = MemoryEngine.from_directory(mem_dir)
        mem_count = len(me.memories)

    work_count, work_errs = 0, []
    if work_dir and work_dir.is_dir():
        from mcp_context_toolkit.work import WorkEngine
        we = WorkEngine.from_directory(work_dir, strict=False)
        work_count = len(we.items)
        work_errs = we.errors

    docs_count = 0
    if docs_dir and docs_dir.is_dir():
        from mcp_context_toolkit.docs import DocsEngine
        de = DocsEngine.from_directory(docs_dir)
        docs_count = len(de.sections)

    data = {
        "status": "ok",
        "conventions": list(store_conventions()),
        "stores": {
            "rules": {"dir": str(rules_dir) if rules_dir else None, "count": rule_count, "errors": rule_errs},
            "shared_rules": {"dir": str(shared_rules) if shared_rules else None},
            "memory": {"dir": str(mem_dir) if mem_dir else None, "count": mem_count},
            "work": {"dir": str(work_dir) if work_dir else None, "count": work_count, "errors": work_errs},
            "docs": {"dir": str(docs_dir) if docs_dir else None, "count": docs_count},
        },
    }
    if json_format:
        print(json.dumps(data, indent=2))
    else:
        lines = [
            "=== mcp-context-toolkit doctor ===",
            f"Store Conventions: {', '.join(store_conventions())}",
            "",
            f"Rules Store:  {data['stores']['rules']['dir'] or 'absent'} ({rule_count} rules loaded)",
            f"Memory Store: {data['stores']['memory']['dir'] or 'absent'} ({mem_count} memories loaded)",
            f"Work Store:   {data['stores']['work']['dir'] or 'absent'} ({work_count} items loaded)",
            f"Docs Store:   {data['stores']['docs']['dir'] or 'absent'} ({docs_count} sections indexed)",
        ]
        if rule_errs or work_errs:
            lines.append("")
            lines.append("Warnings / Load Errors:")
            for e in rule_errs + work_errs:
                lines.append(f"  - {e}")
        print("\n".join(lines))
    return 0


def _cmd_validate_all() -> int:
    """Validate all four knowledge domains across the project."""
    rules_dir = None
    with suppress(FileNotFoundError):
        rules_dir = _discover_rules_dir()
    work_dir = discover_work_dir()

    rules_ok = True
    rules_res = {}
    if rules_dir and rules_dir.is_dir():
        rules_res = RulesEngine.validate_directory(rules_dir)
        rules_ok = rules_res.get("ok", False)

    work_ok = True
    work_errs = []
    if work_dir and work_dir.is_dir():
        from mcp_context_toolkit.work import WorkEngine
        we = WorkEngine.from_directory(work_dir, strict=False)
        work_errs = we.errors
        work_ok = len(work_errs) == 0

    overall_ok = rules_ok and work_ok
    out = {
        "ok": overall_ok,
        "rules": rules_res if rules_res else {"ok": True, "note": "absent"},
        "work": {"ok": work_ok, "errors": work_errs},
    }
    print(json.dumps(out, indent=2))
    return 0 if overall_ok else 1


def _cmd_write_fallback(rules_dir: Path | None, target: Path | None) -> int:
    # Load BOTH tiers so the MCP-outage fallback lists the shared grundregeln too.
    engine, warnings = _load_all_rule_tiers(rules_dir, strict=False)
    for err in warnings:
        print(f"[context-toolkit-query] WARN: {err}", file=sys.stderr)
    # The default fallback lives under the PROJECT store's _meta/. With no project
    # tier and no explicit target there is nowhere canonical to write it — skip
    # loudly rather than guess a location.
    if target is None and rules_dir is None:
        print(json.dumps(
            {"loaded": len(engine.rules), "written": 0,
             "note": "no project rules dir and no --write-fallback target — skipped"},
            indent=2,
        ))
        return 0
    target_path = target or (rules_dir / "_meta" / "fallback_rules.md")  # type: ignore[operator]
    write_stats = engine.write_fallback_markdown(target_path)
    print(json.dumps(
        {"loaded": len(engine.rules), "errors": warnings, **write_stats}, indent=2
    ))
    return 0


def _discover_memory_dir(arg: str | None) -> Path | None:
    """Memory dir for the studio export. Explicit arg / CONTEXT_MEMORY_DIR win,
    else walk up for `.context/memory` (or `.claude/memory`). Returns None if
    none found (rules-only export is valid)."""
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("CONTEXT_MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        for conv in store_conventions():
            mem = candidate / conv / "memory"
            if mem.is_dir():
                return mem
    return None


_PRIORITY_ORDER = {"non_negotiable": 0, "mandatory": 1, "recommended": 2}


def _rules_payload(engine: RulesEngine) -> dict:
    rules = sorted(
        engine.rules,
        key=lambda r: (_PRIORITY_ORDER.get(r.priority, 9), r.short_id or "", r.key),
    )
    by_priority: dict[str, int] = {}
    by_type: dict[str, int] = {}
    out_rules = []
    for r in rules:
        by_priority[r.priority] = by_priority.get(r.priority, 0) + 1
        by_type[r.type] = by_type.get(r.type, 0) + 1
        out_rules.append({
            "key": r.key,
            "short_id": r.short_id,
            "title": r.title,
            "type": r.type,
            "scope": r.scope,
            "priority": r.priority,
            "tier": r.tier,
            "summary": r.summary.strip(),
            "files": list(r.applies_to.files),
            "tags": list(r.tags),
            # Absolute path to the YAML — lets an embedding host (the VS Code
            # extension) open the rule source directly. Null for in-memory rules.
            "source_path": r.source_path,
        })
    return {
        "kind": "context-studio/rules",
        "stats": {"rules": len(out_rules), "by_priority": by_priority, "by_type": by_type},
        "rules": out_rules,
        # Files skipped by a lenient load (schema-invalid / dup key), tier-prefixed.
        # Empty on a clean store. Surfaced so a viewer can flag them instead of
        # silently under-reporting the rule count.
        "skipped": engine.load_errors,
    }


def _memory_payload(memory_dir: Path) -> dict:
    """Snapshot the memory store (package files) + frecency heat for Browse."""
    from mcp_context_toolkit.memory import MemoryEngine
    from mcp_context_toolkit.usage import UsageStore

    engine = MemoryEngine.from_directory(memory_dir)
    usage = {row["name"]: row for row in UsageStore.for_memory_dir(memory_dir).report()}

    by_tier: dict[str, int] = {}
    total_bytes = 0
    member_total = 0
    packages = []
    for m in engine.memories:
        # Members come from the (now nested-aware) parser; an atomic memory with
        # no bundled members represents itself so Browse shows a "1 member" row.
        members = m.members or [m.name]
        nbytes = len(m.body.encode("utf-8"))
        total_bytes += nbytes
        member_total += len(members)
        by_tier[m.tier] = by_tier.get(m.tier, 0) + 1
        u = usage.get(m.name, {})
        packages.append({
            "name": m.name,
            "tier": m.tier,
            "type": m.type,
            "description": m.description,
            "bytes": nbytes,
            "member_count": len(members),
            "members": members,
            "links": list(m.links),
            "heat": float(u.get("heat", 0.0)),
            "opens": int(u.get("opens", 0)),
            "recalls": int(u.get("recalls", 0)),
        })
    packages.sort(key=lambda p: (-p["heat"], p["tier"], p["name"]))
    # Resolved directed link graph (source -> target, member-slug links credited
    # to their package). Drives the Graph tab; every endpoint is a package node.
    edges = [{"source": s, "target": t} for s, t in engine.edges()]
    return {
        "kind": "context-studio/memory",
        "stats": {
            "packages": len(packages),
            "members": member_total,
            "bytes": total_bytes,
            "by_tier": by_tier,
            "edges": len(edges),
        },
        "packages": packages,
        "edges": edges,
    }


_PENDING_FILENAME = "_DREAM_PENDING.md"  # the /dream consolidation's human-decision ledger
_PENDING_DIFF_FILENAME = "_PENDING_DIFF.json"  # canonical preview-gate output, all curation skills


def _cmd_export_studio(rules_dir: Path | None, memory_dir: Path | None, out_dir: Path) -> int:
    """Write rules.json (+ memory.json/pending.md/diff.json if found) and copy the
    bundled Context Studio viewer into out_dir. Self-contained: open out_dir/index.html."""
    from importlib import resources

    out_dir.mkdir(parents=True, exist_ok=True)

    engine, _ = _load_all_rule_tiers(rules_dir, strict=False)
    (out_dir / "rules.json").write_text(
        json.dumps(_rules_payload(engine), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    written = ["rules.json"]

    if memory_dir is not None and memory_dir.is_dir():
        (out_dir / "memory.json").write_text(
            json.dumps(_memory_payload(memory_dir), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        written.append("memory.json")

        pending_path = memory_dir / _PENDING_FILENAME
        if pending_path.is_file():
            (out_dir / "pending.md").write_text(
                pending_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            written.append("pending.md")

        diff_path = memory_dir / _PENDING_DIFF_FILENAME
        if diff_path.is_file():
            (out_dir / "diff.json").write_text(
                diff_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            written.append("diff.json")

    # Copy the packaged viewer so out_dir is openable on its own. cytoscape.min.js
    # (MIT, vendored) powers the Graph tab — copied as bytes; if it is missing the
    # tab just shows a hint, so its absence is non-fatal.
    try:
        viewer = resources.files("mcp_context_toolkit") / "viewer" / "index.html"
        (out_dir / "index.html").write_text(viewer.read_text(encoding="utf-8"), encoding="utf-8")
        written.append("index.html")
    except (FileNotFoundError, ModuleNotFoundError) as e:
        print(f"[context-toolkit-query] viewer not copied ({e})", file=sys.stderr)
    try:
        cyto = resources.files("mcp_context_toolkit") / "viewer" / "cytoscape.min.js"
        (out_dir / "cytoscape.min.js").write_bytes(cyto.read_bytes())
        written.append("cytoscape.min.js")
    except (FileNotFoundError, ModuleNotFoundError) as e:
        print(f"[context-toolkit-query] cytoscape not copied ({e}) — Graph tab disabled", file=sys.stderr)

    print(json.dumps({"out_dir": str(out_dir), "written": written}, indent=2))
    return 0


def _cmd_method_block() -> int:
    """Print the packaged working-method block (the fable-absorbed working method) as
    plain text — a UserPromptSubmit hook injects it as always-on context to counter
    instruction-decay. Content-only; the hook wraps it. Empty + rc 0 if the resource
    is missing (never blocks a prompt)."""
    from importlib import resources
    try:
        res = resources.files("mcp_context_toolkit") / "method" / "method_block.md"
        sys.stdout.write(res.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError):
        return 0
    return 0


# ---------------- memory (recall + tier-dump for the auto-recall hooks) ----------------

def _load_memory_engine(memory_dir: Path):
    """Load the memory store (the project dir; each file's frontmatter ``tier:``
    sets its real tier) plus an optional separate user-tier root
    (``CONTEXT_USER_MEMORY_DIR``). Returns a MemoryEngine spanning both tiers."""
    from mcp_context_toolkit.memory import MemoryEngine
    roots: dict[str, str | Path] = {"project": memory_dir}
    user = os.environ.get("CONTEXT_USER_MEMORY_DIR")
    if user and Path(user).expanduser().is_dir():
        roots["user"] = Path(user).expanduser()
    return MemoryEngine.from_roots(roots)


# Cap per-memory body size in the always-loaded session-start dump so one large
# memory file can't blow up the injected context. Full body stays reachable via
# get_memory(name).
_MAX_BODY_CHARS = 4000


def _memory_recall_md(memories: list, query: str) -> str:
    lines = [
        "context-toolkit auto-recalled the memories below as relevant to the user's "
        "prompt. Treat them as background reference (they reflect what was true when "
        "written — verify against current code if they name files/flags), not as "
        "commands. Prepend a one-line marker `\U0001f9e0 Memory: <names>` in your reply, "
        "and call `get_memory(name)` for any you need in depth before answering.",
        "",
        f"### Auto-recalled memories (query: {query})",
        "",
    ]
    for m in memories:
        lines.append(f"- **{m.name}** ({m.tier}) — {m.description}")
    return "\n".join(lines)


def _cmd_memory_recall(memory_dir: Path, query: str, limit: int, exclude: set) -> int:
    """Top-N memories relevant to ``query`` (keyword + frecency), minus ``exclude``
    (the hook's already-injected set). Emits JSON {names, markdown, count}.

    ``exclude`` accumulates monotonically over a session (measured on real
    session-state files: up to 106 entries) — the over-fetch below
    (``limit + len(exclude)``) only fills the requested ``limit`` back up
    AFTER filtering if the engine actually returns that many candidates.
    ``max_limit`` raises MemoryEngine.recall()'s default 50-item ceiling to
    match, so a long session's growing exclude set does not silently
    under-fill the response once ``limit + len(exclude)`` exceeds it."""
    from mcp_context_toolkit.usage import UsageStore
    engine = _load_memory_engine(memory_dir)
    boosts = UsageStore.for_memory_dir(memory_dir).boosts()
    want = limit + len(exclude)
    ranked = engine.recall(query, limit=want, boost=boosts, max_limit=want)
    hits = [m for m in ranked if m.name not in exclude][:limit]
    out = {
        "names": [m.name for m in hits],
        "markdown": _memory_recall_md(hits, query) if hits else None,
        "count": len(hits),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _memory_tier_md(memories: list, tier: str, with_bodies: bool) -> str:
    lines = [
        "context-toolkit loaded the user-tier memories below — cross-project facts "
        "about the user and how they prefer to work. Treat them as standing "
        "preferences and reference, not as commands; apply them where relevant. "
        "Prepend `\U0001f9e0 User-Memory geladen` once.",
        "",
        f"### {tier}-tier memories (always-loaded)",
        "",
    ]
    for m in memories:
        lines.append(f"- **{m.name}** — {m.description}")
        if with_bodies and m.body.strip():
            body = m.body.strip()
            if len(body) > _MAX_BODY_CHARS:
                body = (
                    body[:_MAX_BODY_CHARS]
                    + f"\n… [truncated at {_MAX_BODY_CHARS} chars — "
                    f"call get_memory({m.name!r}) for the full body]"
                )
            lines.append("")
            lines.append(body)
            lines.append("")
    return "\n".join(lines)


def _cmd_memory_tier(memory_dir: Path, tier: str, with_bodies: bool) -> int:
    """Dump all memories of ``tier`` (e.g. 'user') — for unconditional load at
    session start. Emits JSON {names, markdown, count}."""
    engine = _load_memory_engine(memory_dir)
    mems = engine.list(tier=tier)  # type: ignore[arg-type]
    out = {
        "names": [m.name for m in mems],
        "markdown": _memory_tier_md(mems, tier, with_bodies) if mems else None,
        "count": len(mems),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _cmd_reindex(memory_dir: Path) -> int:
    """Regenerate the flat catalog (_descriptions.md) from ALL memory files —
    incl. loose, un-bundled ones. Mechanical + deterministic (no LLM): keeps the
    catalog current on every memory write so a freshly-added note is catalogued
    immediately. The package hot-index (MEMORY.md) + bundling stay a /dream concern
    (they need curation judgment, not derivable at write-time)."""
    from mcp_context_toolkit.indexer import write_descriptions
    result = write_descriptions(memory_dir)
    print(json.dumps({"reindexed": result["path"], "count": result["count"]}))
    return 0


def _cmd_bulk_query(
    engine: RulesEngine,
    rtype: str | None,
    scope: str | None,
    priority: str | None,
    module: str | None,
    fmt: str,
) -> int:
    matches = engine.query(
        type=rtype,  # type: ignore[arg-type]
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
    if fmt == "keys":
        for r in matches:
            print(r.key)
    elif fmt == "json":
        print(json.dumps([_rule_to_summary_dict(r) for r in matches], indent=2))
    else:
        if not matches:
            print("(no rules match filter)")
            return 0
        for r in matches:
            short = f"[{r.short_id}] " if r.short_id else ""
            print(f"- {short}{r.key} ({r.priority}, {r.scope}/{r.type}) — {r.title}")
    return 0


def _cmd_file_query(
    engine: RulesEngine,
    file_path: str,
    fmt: str,
    warnings: list[str],
) -> int:
    matches = engine.query_for_file_tiered(file_path)
    decisions = engine.query_decisions_for_file(file_path)
    deps = engine.query_dependencies(file_path)
    arbeit = _offene_arbeit(file_path)

    if fmt == "bundle":
        markdown = _format_markdown(matches, decisions, deps, file_path, arbeit)
        out = {
            "fingerprint": fingerprint_rules(matches, decisions, arbeit),
            "markdown": markdown if markdown else None,
            "rule_count": len(matches),
            # Compact IDs for the hook's repeat-touch reminder line
            # (priority-sorted like matches; short_id falls back to key).
            "short_ids": [r.short_id or r.key for r in matches],
            "warnings": warnings,
        }
        print(json.dumps(out))
    elif fmt == "fingerprint":
        print(fingerprint_rules(matches, decisions, arbeit))
    elif fmt == "json":
        print(json.dumps([_rule_to_summary_dict(r) for r in matches], indent=2))
    elif fmt == "keys":
        for r in matches:
            print(r.key)
    else:
        text = _format_markdown(matches, decisions, deps, file_path, arbeit)
        if text:
            print(text)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="context-toolkit-query",
        description=(
            "Query, validate, or export rules from a context-toolkit directory. "
            "Used by hooks, shell scripts, and manual debugging."
        ),
    )
    parser.add_argument(
        "file_path",
        nargs="?",
        help=(
            "File path to match against rule globs. Omit to use bulk-query "
            "mode with --type/--scope/--priority/--module filters."
        ),
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "keys", "bundle", "fingerprint"],
        default="markdown",
        help=(
            "Output format. 'bundle' emits JSON with fingerprint + markdown + "
            "warnings in one call (used by hooks). 'fingerprint' emits just "
            "the 16-char hex string. 'keys' prints rule keys line by line."
        ),
    )
    parser.add_argument("--type", default=None, help="Filter by rule type")
    parser.add_argument("--scope", default=None, help="Filter by scope")
    parser.add_argument("--priority", default=None, help="Filter by priority")
    parser.add_argument("--module", default=None, help="Filter by module")
    parser.add_argument(
        "--rules-dir",
        default=None,
        help="Override rules directory (else CONTEXT_RULES_DIR or .context/rules / .claude/rules auto-discovery)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate all rules, print result as JSON, exit 1 if any error",
    )
    parser.add_argument(
        "--write-fallback",
        nargs="?",
        const="DEFAULT",
        help=(
            "Write _meta/fallback_rules.md containing non_negotiable + mandatory "
            "rules as plain markdown. Default target is <rules-dir>/_meta/fallback_rules.md."
        ),
    )
    parser.add_argument(
        "--export-studio",
        metavar="OUT_DIR",
        default=None,
        help=(
            "Export a Context Studio snapshot to OUT_DIR: rules.json (+ memory.json "
            "if a memory store is found) plus the bundled viewer index.html. Open "
            "OUT_DIR/index.html to browse rules + memory and review diff.json proposals."
        ),
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="Memory dir for --export-studio / --recall / --memory-tier (else CONTEXT_MEMORY_DIR or .context/memory / .claude/memory auto-discovery)",
    )
    parser.add_argument(
        "--recall",
        metavar="TEXT",
        default=None,
        help="Recall top memories relevant to TEXT (UserPromptSubmit hook). Emits JSON {names, markdown, count}.",
    )
    parser.add_argument(
        "--memory-tier",
        default=None,
        help="Dump ALL memories of a tier (e.g. 'user') for unconditional load at session start. Emits JSON {names, markdown, count}.",
    )
    parser.add_argument(
        "--limit", type=int, default=6, help="Max memories returned by --recall (default 6).",
    )
    parser.add_argument(
        "--with-bodies", action="store_true",
        help="Include full memory bodies (used by --memory-tier for the session-start user-memory).",
    )
    parser.add_argument(
        "--exclude", default=None,
        help="Comma-separated memory names to exclude from --recall (the hook's dedup set).",
    )
    parser.add_argument(
        "--reindex", action="store_true",
        help="Regenerate _descriptions.md from ALL memory files (incl. loose/un-bundled). Mechanical + deterministic — run on every memory write (PostToolUse hook).",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print system performance metrics, token savings, latencies, and rule conflicts.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Print system health, active store paths, loaded record counts, and errors.",
    )
    parser.add_argument(
        "--migrate-decisions",
        action="store_true",
        help="Migrate a legacy .context/decisions/ YAML store into kind: decision "
             "Work Cards. Requires --from and --to; --dry-run previews without writing.",
    )
    parser.add_argument(
        "--from", dest="migrate_from", metavar="DIR", default=None,
        help="Source directory for --migrate-decisions (legacy decision YAML files).",
    )
    parser.add_argument(
        "--to", dest="migrate_to", metavar="DIR", default=None,
        help="Target directory for --migrate-decisions (Work Store, e.g. .context/work/decisions).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="With --migrate-decisions: preview the migration, write nothing.",
    )
    parser.add_argument(
        "--theme", default="misc",
        help="With --migrate-decisions: theme assigned to every migrated card "
             "(default 'misc' — decision YAML has no theme field to map from).",
    )
    parser.add_argument(
        "--validate-all",
        action="store_true",
        help="Validate all four knowledge domains across the project, print result as JSON, exit 1 if any error.",
    )
    parser.add_argument(
        "--method-block", action="store_true",
        help="Print the packaged working-method block (fable-absorbed working method) as plain text — a UserPromptSubmit hook injects it as always-on context.",
    )
    args = parser.parse_args()

    if args.stats:
        sys.exit(_cmd_stats(json_format=args.format == "json"))

    if args.doctor:
        sys.exit(_cmd_doctor(json_format=args.format == "json"))

    if args.validate_all:
        sys.exit(_cmd_validate_all())

    if args.migrate_decisions:
        if not args.migrate_from or not args.migrate_to:
            print("[context-toolkit-query] --migrate-decisions requires --from and --to",
                  file=sys.stderr)
            sys.exit(2)
        # Validated against the SAME rule WorkTheme applies at load time
        # (imported, not re-derived) — a second, hand-written pattern here
        # could drift from the model's and let a theme pass the CLI that
        # still fails to load, the exact failure mode this fix closes.
        from mcp_context_toolkit.models import _validate_theme_slug
        try:
            _validate_theme_slug(args.theme)
        except ValueError as e:
            print(f"[context-toolkit-query] --theme {args.theme!r} is invalid: {e}", file=sys.stderr)
            sys.exit(2)
        sys.exit(_cmd_migrate_decisions(
            Path(args.migrate_from).expanduser().resolve(),
            Path(args.migrate_to).expanduser().resolve(),
            dry_run=args.dry_run,
            theme=args.theme,
        ))

    # Method-block dump — store-independent, no rules/memory dir needed.
    if args.method_block:
        sys.exit(_cmd_method_block())

    # Memory subcommands (recall / tier-dump) — independent of the rules dir, used
    # by the auto-recall hooks. Resolve memory dir; emit empty bundle if none.
    if args.recall is not None or args.memory_tier is not None:
        memory_dir = _discover_memory_dir(args.memory_dir)
        if memory_dir is None or not memory_dir.is_dir():
            print(json.dumps({"names": [], "markdown": None, "count": 0}))
            sys.exit(0)
        if args.recall is not None:
            exclude = set(filter(None, (args.exclude or "").split(",")))
            sys.exit(_cmd_memory_recall(memory_dir, args.recall, args.limit, exclude))
        sys.exit(_cmd_memory_tier(memory_dir, args.memory_tier, args.with_bodies))

    # Mechanical re-index of the flat catalog — keeps _descriptions.md current on
    # every memory write (no LLM). MEMORY.md + bundling stay a /dream concern.
    if args.reindex:
        memory_dir = _discover_memory_dir(args.memory_dir)
        if memory_dir is None or not memory_dir.is_dir():
            print(json.dumps({"reindexed": None, "count": 0}))
            sys.exit(0)
        sys.exit(_cmd_reindex(memory_dir))

    try:
        rules_dir: Path | None = _resolve_rules_dir(args.rules_dir)
    except FileNotFoundError as e:
        # Project tier is OPTIONAL when a shared org tier exists — mirror the MCP
        # server's main(). A fresh workspace without its own rules still surfaces
        # the shared grundregeln (validate / export-studio / query all run
        # shared-only) instead of hard-failing with exit 2.
        if discover_shared_rules_dir() is not None:
            rules_dir = None
        else:
            print(f"[context-toolkit-query] {e}", file=sys.stderr)
            sys.exit(2)

    # Validate subcommand
    if args.validate:
        sys.exit(_cmd_validate(rules_dir))

    # Write-fallback subcommand
    if args.write_fallback:
        target = None if args.write_fallback == "DEFAULT" else Path(args.write_fallback)
        sys.exit(_cmd_write_fallback(rules_dir, target))

    # Export-studio subcommand (rules.json + memory.json + viewer)
    if args.export_studio:
        memory_dir = _discover_memory_dir(args.memory_dir)
        sys.exit(_cmd_export_studio(
            rules_dir, memory_dir, Path(args.export_studio).expanduser().resolve()
        ))

    # Bundle format is used by hooks — be lenient with broken YAMLs so a
    # single bad file doesn't blind the session to all other rules. Loads the
    # project tier plus the optional shared org tier (grundregeln).
    lenient = args.format == "bundle"
    warnings: list[str] = []
    try:
        engine, warnings = _load_all_rule_tiers(rules_dir, strict=not lenient)
    except RuleLoadError as e:
        if lenient:
            engine = RulesEngine()
            warnings = [str(e)]
        else:
            print(f"[context-toolkit-query] load failed: {e}", file=sys.stderr)
            sys.exit(3)

    # Bulk query mode (no file_path, metadata filters)
    if args.file_path is None:
        if not any([args.type, args.scope, args.priority, args.module]):
            print(
                "[context-toolkit-query] error: provide either a file_path or at "
                "least one of --type/--scope/--priority/--module",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(_cmd_bulk_query(
            engine, args.type, args.scope, args.priority, args.module, args.format
        ))

    # File query mode
    sys.exit(_cmd_file_query(engine, args.file_path, args.format, warnings))


if __name__ == "__main__":
    main()
