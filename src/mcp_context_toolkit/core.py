"""Shared primitives for knowledge stores (rules + memory).

Both rules and memory are the same shape on disk — a directory of files with
YAML frontmatter that grows and changes over time. This module holds the
genuinely shared, content-type-agnostic helpers so a new store type (memory
today, anything markdown-with-frontmatter tomorrow) does not re-invent them.

Deliberately tiny and dependency-light: no host-application knowledge, no rule/memory
specifics. The rule engine (engine.py) and memory engine (memory.py) build
their own models + query semantics on top.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import yaml


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
