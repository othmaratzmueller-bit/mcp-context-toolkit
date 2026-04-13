from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from claude_rules.engine import (
    RuleLoadError,
    RulesEngine,
    fingerprint_rules,
)


def _discover_rules_dir() -> Path:
    env = os.environ.get("CLAUDE_RULES_DIR")
    if env:
        return Path(env).expanduser().resolve()

    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        rules = candidate / ".claude" / "rules"
        if rules.is_dir():
            return rules

    raise FileNotFoundError(
        "No rules directory found. Set CLAUDE_RULES_DIR or create .claude/rules/."
    )


def _format_markdown(rules: list, file_path: str) -> str:
    if not rules:
        return ""
    lines = [
        "INSTRUCTION TO ASSISTANT: The user is working on a file with active "
        "rules from the claude-rules pilot. In your next user-facing response, "
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
        f"_{len(rules)} rule(s) loaded. Use `get_rule(key)` via the claude-rules "
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


def _resolve_rules_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    return _discover_rules_dir()


def _cmd_validate(rules_dir: Path) -> int:
    result = RulesEngine.validate_directory(rules_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _cmd_write_fallback(rules_dir: Path, target: Path | None) -> int:
    engine = RulesEngine()
    stats = engine.load_directory(rules_dir, strict=False)
    if stats.get("errors"):
        for err in stats["errors"]:
            print(f"[claude-rules-query] WARN: {err}", file=sys.stderr)
    target_path = target or (rules_dir / "_meta" / "fallback_rules.md")
    write_stats = engine.write_fallback_markdown(target_path)
    print(json.dumps({**stats, **write_stats}, indent=2))
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
        prog="claude-rules-query",
        description=(
            "Query, validate, or export rules from a claude-rules directory. "
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
        help="Override rules directory (else CLAUDE_RULES_DIR or .claude/rules auto-discovery)",
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
    args = parser.parse_args()

    try:
        rules_dir = _resolve_rules_dir(args.rules_dir)
    except FileNotFoundError as e:
        print(f"[claude-rules-query] {e}", file=sys.stderr)
        sys.exit(2)

    # Validate subcommand
    if args.validate:
        sys.exit(_cmd_validate(rules_dir))

    # Write-fallback subcommand
    if args.write_fallback:
        target = None if args.write_fallback == "DEFAULT" else Path(args.write_fallback)
        sys.exit(_cmd_write_fallback(rules_dir, target))

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
            print(f"[claude-rules-query] load failed: {e}", file=sys.stderr)
            sys.exit(3)

    # Bulk query mode (no file_path, metadata filters)
    if args.file_path is None:
        if not any([args.type, args.scope, args.priority, args.module]):
            print(
                "[claude-rules-query] error: provide either a file_path or at "
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
