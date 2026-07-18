"""Shared primitives for knowledge stores (rules + memory).

Both rules and memory are the same shape on disk — a directory of files with
YAML frontmatter that grows and changes over time. This module holds the
genuinely shared, content-type-agnostic helpers so a new store type (memory
today, anything markdown-with-frontmatter tomorrow) does not re-invent them.

Also home to store-directory conventions and discovery helpers shared between
the MCP server and the CLI — kept here so they stay in sync by construction
rather than by comment ("keep in sync with …").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import yaml

# Store-directory conventions tried during walk-up auto-discovery, in order.
# ``.context`` is the generic default (matches the tool/env naming); ``.claude``
# is kept as a fallback for existing Claude Code repos. Overridable via
# CONTEXT_STORE_CONVENTIONS (comma-separated, e.g. ".acme,.context,.claude")
# so an embedding product can brand its store dir without forking the engine.
_DEFAULT_STORE_CONVENTIONS = (".context", ".claude")


class KnowledgeLoadError(Exception):
    """A knowledge file (rule or memory) failed to parse or validate."""

    def __init__(self, file: Path, errors: str):
        super().__init__(f"{file}: {errors}")
        self.file = file
        self.errors = errors


def iter_files(root: Path, suffix: str) -> Iterator[Path]:
    """Yield files ending in ``suffix`` under ``root``, sorted, skipping any
    path whose parts start with ``_`` (meta dirs like ``_meta``/``_flushed``)."""
    for path in sorted(root.rglob(f"*{suffix}")):
        if any(part.startswith("_") for part in path.relative_to(root).parts):
            continue
        yield path


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split markdown into ``(frontmatter mapping, body)``.

    Frontmatter is a leading block delimited by lines containing only ``---``.
    Returns ``({}, text)`` when no well-formed frontmatter is present. Never
    raises on bad YAML — returns ``({}, text)`` so a single malformed file is
    degraded, not fatal (memory is best-effort, unlike the strict rule schema).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


# ---------------------------------------------------------------------------
# Store-directory conventions + discovery — shared between MCP server and CLI
# ---------------------------------------------------------------------------


def store_conventions() -> tuple[str, ...]:
    """Return the ordered store-directory conventions to try during walk-up
    auto-discovery. Defaults to ``(".context", ".claude")``; overridable via
    the ``CONTEXT_STORE_CONVENTIONS`` env var (comma-separated)."""
    raw = os.environ.get("CONTEXT_STORE_CONVENTIONS", "").strip()
    if not raw:
        return _DEFAULT_STORE_CONVENTIONS
    parts = tuple(p.strip() for p in raw.split(",") if p.strip())
    return parts or _DEFAULT_STORE_CONVENTIONS


def discover_shared_rules_dir() -> Path | None:
    """Locate the optional SHARED (org) rules tier. Env-only
    (``CONTEXT_SHARED_RULES_DIR``), NO walk-up — the shared tier is an explicit
    deployment choice (org grundregeln mounted read-only), never auto-found in a
    parent dir the way the project tier is. Returns ``None`` when unset."""
    env = os.environ.get("CONTEXT_SHARED_RULES_DIR")
    if not env:
        return None
    p = Path(env).expanduser().resolve()
    return p if p.is_dir() else None


def warn_if_example_rules(rules_dir: Path) -> None:
    """The shipped starter pack (``examples/rules``) is INERT — copy-to-activate,
    never a production default. Auto-discovery (``<dir>/.context/rules`` or
    ``<dir>/.claude/rules``) can never reach it, but a misconfigured
    ``CONTEXT_RULES_DIR`` could point at it directly. Print a loud NOTE so the
    examples don't silently 'poison' a real rule set."""
    if "examples" in rules_dir.parts:
        import sys

        print(
            f"[context-toolkit] NOTE: serving EXAMPLE/starter rules from "
            f"{rules_dir} — inert starter pack, copy into your own rules "
            f"dir for real use.",
            file=sys.stderr,
        )
