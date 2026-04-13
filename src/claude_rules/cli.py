from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from claude_rules.engine import RulesEngine


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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-rules-query",
        description="Query rules for a file path (one-shot, used by hooks).",
    )
    parser.add_argument("file_path", help="File path to match against rule globs")
    parser.add_argument(
        "--format",
        choices=["markdown", "json", "keys"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--rules-dir",
        default=None,
        help="Override rules directory (else CLAUDE_RULES_DIR or .claude/rules auto-discovery)",
    )
    args = parser.parse_args()

    try:
        rules_dir = Path(args.rules_dir).expanduser().resolve() if args.rules_dir else _discover_rules_dir()
    except FileNotFoundError as e:
        print(f"[claude-rules-query] {e}", file=sys.stderr)
        sys.exit(2)

    engine = RulesEngine.from_directory(rules_dir)
    matches = engine.query_for_file(args.file_path)

    if args.format == "json":
        out = [
            {
                "key": r.key,
                "short_id": r.short_id,
                "title": r.title,
                "priority": r.priority,
                "scope": r.scope,
                "type": r.type,
                "summary": r.summary.strip(),
            }
            for r in matches
        ]
        print(json.dumps(out, indent=2))
    elif args.format == "keys":
        for r in matches:
            print(r.key)
    else:
        text = _format_markdown(matches, args.file_path)
        if text:
            print(text)


if __name__ == "__main__":
    main()
