from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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
        rules = candidate / ".claude" / "rules"
        if rules.is_dir():
            return rules

    raise FileNotFoundError(
        "No rules directory found. Set CONTEXT_RULES_DIR or create .claude/rules/."
    )


def _format_markdown(rules: list, file_path: str) -> str:
    if not rules:
        return ""
    lines = [
        "INSTRUCTION TO ASSISTANT: The user is working on a file with active "
        "rules from the context-toolkit pilot. In your next user-facing response, "
        "prepend a one-line marker noting which rule short-ids apply to this "
        "file, e.g. `📋 Rules aktiv: S1, Q1 (backend/path.py)` — this is the "
        "transparency requirement for the pilot phase. Then follow the rules "
        "while making the edit. Full rule text is shown below for your reference.",
        "",
        f"### Active rules for `{file_path}`",
        "",
    ]
    for r in rules:
        short = f" [{r.short_id}]" if r.short_id else ""
        lines.append(f"- **{r.priority}** `{r.key}`{short} — {r.title}")
        summary = r.summary.strip().replace("\n", " ")
        if len(summary) > 160:
            summary = summary[:157] + "..."
        lines.append(f"  {summary}")
    lines.append("")
    lines.append(
        f"_{len(rules)} rule(s) loaded. Use `get_rule(key)` via the context-toolkit "
        "MCP tool for full content._"
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


def _warn_if_example_rules(rules_dir: Path) -> None:
    """The shipped starter pack (``examples/rules``) is INERT — copy-to-activate,
    never a production default. Auto-discovery (``<dir>/.claude/rules``) can never
    reach it, but a misconfigured ``CONTEXT_RULES_DIR`` / ``--rules-dir`` could
    point at it directly. Say so loudly so the examples don't silently 'poison'
    a real rule set."""
    if "examples" in rules_dir.parts:
        print(
            f"[context-toolkit-query] NOTE: loading EXAMPLE/starter rules from "
            f"{rules_dir}. These are an inert starter pack — copy them into your "
            f"own rules directory for real use.",
            file=sys.stderr,
        )


def _resolve_rules_dir(arg: str | None) -> Path:
    if arg:
        resolved = Path(arg).expanduser().resolve()
    else:
        resolved = _discover_rules_dir()
    _warn_if_example_rules(resolved)
    return resolved


def _cmd_validate(rules_dir: Path) -> int:
    result = RulesEngine.validate_directory(rules_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _cmd_write_fallback(rules_dir: Path, target: Path | None) -> int:
    engine = RulesEngine()
    stats = engine.load_directory(rules_dir, strict=False)
    if stats.get("errors"):
        for err in stats["errors"]:
            print(f"[context-toolkit-query] WARN: {err}", file=sys.stderr)
    target_path = target or (rules_dir / "_meta" / "fallback_rules.md")
    write_stats = engine.write_fallback_markdown(target_path)
    print(json.dumps({**stats, **write_stats}, indent=2))
    return 0


def _discover_memory_dir(arg: str | None) -> Path | None:
    """Memory dir for the studio export. Explicit arg / CONTEXT_MEMORY_DIR win,
    else walk up for `.claude/memory`. Returns None if none found (rules-only
    export is valid)."""
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("CONTEXT_MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        mem = candidate / ".claude" / "memory"
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
            "summary": r.summary.strip(),
            "files": list(r.applies_to.files),
            "tags": list(r.tags),
        })
    return {
        "kind": "context-studio/rules",
        "stats": {"rules": len(out_rules), "by_priority": by_priority, "by_type": by_type},
        "rules": out_rules,
    }


def _package_members(source_path: str | None, fallback_name: str) -> list[str]:
    """Authoritative member list = the package's frontmatter `members:` list.
    Falls back to the record's own name for atomic (non-bundled) memory files —
    NOT a `## header` scan, which over-counts markdown sub-headers inside bodies."""
    if source_path:
        try:
            from mcp_context_toolkit.core import parse_frontmatter
            meta, _ = parse_frontmatter(Path(source_path).read_text(encoding="utf-8"))
            members = meta.get("members")
            if isinstance(members, list) and members:
                return [str(x) for x in members]
        except OSError:
            pass
    return [fallback_name]


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
        members = _package_members(m.source_path, m.name)
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
    return {
        "kind": "context-studio/memory",
        "stats": {
            "packages": len(packages),
            "members": member_total,
            "bytes": total_bytes,
            "by_tier": by_tier,
        },
        "packages": packages,
    }


def _cmd_export_studio(rules_dir: Path, memory_dir: Path | None, out_dir: Path) -> int:
    """Write rules.json (+ memory.json if a store is found) and copy the bundled
    Context Studio viewer into out_dir. Self-contained: open out_dir/index.html."""
    from importlib import resources

    out_dir.mkdir(parents=True, exist_ok=True)

    engine = RulesEngine()
    engine.load_directory(rules_dir, strict=False)
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

    # Copy the packaged viewer so out_dir is openable on its own.
    try:
        viewer = resources.files("mcp_context_toolkit") / "viewer" / "index.html"
        (out_dir / "index.html").write_text(viewer.read_text(encoding="utf-8"), encoding="utf-8")
        written.append("index.html")
    except (FileNotFoundError, ModuleNotFoundError) as e:
        print(f"[context-toolkit-query] viewer not copied ({e})", file=sys.stderr)

    print(json.dumps({"out_dir": str(out_dir), "written": written}, indent=2))
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
    matches = engine.query_for_file(file_path)

    if fmt == "bundle":
        markdown = _format_markdown(matches, file_path)
        out = {
            "fingerprint": fingerprint_rules(matches),
            "markdown": markdown if markdown else None,
            "rule_count": len(matches),
            "warnings": warnings,
        }
        print(json.dumps(out))
    elif fmt == "fingerprint":
        print(fingerprint_rules(matches))
    elif fmt == "json":
        print(json.dumps([_rule_to_summary_dict(r) for r in matches], indent=2))
    elif fmt == "keys":
        for r in matches:
            print(r.key)
    else:
        text = _format_markdown(matches, file_path)
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
        help="Override rules directory (else CONTEXT_RULES_DIR or .claude/rules auto-discovery)",
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
        help="Memory dir for --export-studio (else CONTEXT_MEMORY_DIR or .claude/memory auto-discovery)",
    )
    args = parser.parse_args()

    try:
        rules_dir = _resolve_rules_dir(args.rules_dir)
    except FileNotFoundError as e:
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
    # single bad file doesn't blind the session to all other rules.
    lenient = args.format == "bundle"
    warnings: list[str] = []
    engine = RulesEngine()
    try:
        stats = engine.load_directory(rules_dir, strict=not lenient)
        warnings = stats.get("errors", [])
    except RuleLoadError as e:
        if lenient:
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
