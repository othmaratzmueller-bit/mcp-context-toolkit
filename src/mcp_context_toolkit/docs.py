"""Docs content-type for the knowledge engine (Work-Record
`doku_lesend_indexieren_statt_zerreissen`, Etappe F of `mcp_gesamtsprint_
ein_speicher`).

Fourth domain, same architectural pattern as rules/memory/work-store (git
files + a read-only query layer, no new backend) — but a DIFFERENT shape on
purpose: prose documentation (`docs/**/*.md`) stays exactly what it is,
files with headings, not frontmatter-tagged records. Owner decision
(2026-08-19): make it SEARCHABLE, never tear it into cards — the governance
corpus (ISO/AI-Act) must stay citable as documents for the
`audit-keeper` drift mechanism and for auditors, and most prose docs
have no natural per-idea boundary the way a Work-Store record does.

A file is split into SECTIONS by its own Markdown headings (`^#{1,6} `) —
the citation unit is "file + section", not "whole file" (a 400-line
architecture doc is not one hit) and not "paragraph" (that would be the
tearing-into-cards this record explicitly rejected). Read-only, same as
memory.py/work.py: this engine never writes.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from mcp_context_toolkit.core import bm25_scores, build_bm25_index, iter_files

# BM25F field weights — a heading hit counts more than a body hit, same
# reasoning as memory.py's _W_NAME vs _W_BODY (a term in the label is a
# stronger signal than the same term buried in prose).
_W_HEADING, _W_BODY = 2.0, 1.0
# Same floor as memory.py/work.py — see their docstrings for why a fixed
# ratio of the top score, not an absolute cutoff, drops weak near-misses.
_RECALL_FLOOR_RATIO = 0.25
# Upper clamp for `limit` (see recall()'s docstring for the incident this
# closes) — generous enough that no real caller needs more in one call, far
# below the "return most of the corpus" scale a bad/negative value hit.
_MAX_LIMIT = 50

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

# A path segment named EXACTLY "archive" (case-insensitive, W8-Reviewer-Fund
# ERROR-2: a substring match also caught `eval-archive/`, the DELIBERATE,
# actively-used drop location for eval results — not a frozen duplicate,
# measured 21 files wrongly excluded) or matching a bare YYYY-MM-DD (a dated
# snapshot dir, e.g. `governance/audit/2026-06-01/`) is a FROZEN historical
# copy, not the live document — indexing it lets a recall() hit rank a
# snapshot equal to the current file it was frozen from (W8-Reviewer-Fund
# WARNING-2, Etappe F: `governance/Sicherheitsarchitektur_Uebersicht.md` and
# its 2026-06-01 snapshot both surfaced for the same query, one of them
# silently stale). `audit-keeper` already treats `audit/<date>/` as
# untouchable evidence for the same reason — this mirrors that convention
# instead of inventing a second one.
_DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# A directory named "Entscheidung <...>" is an unsigned decision-bundle
# DRAFT (W8-Reviewer-Fund WARNING-4, Etappe F, Owner-decision 2026-08-20):
# `docs/Entscheidung GF 01.07.2026/governance-neufassung/` outranked the
# live `governance/Sicherheitsarchitektur_Uebersicht.md` for its own title
# query — the folder's own 00_README.md declares "Status: ENTWURF zur
# Freigabe" (placeholders unfilled, unsigned), not settled governance. Path-
# based exclusion, not content-sniffed (chosen over parsing a status marker
# out of the text — simpler, and the naming convention is a real project
# convention for decision bundles, not a one-off).
_DRAFT_BUNDLE_RE = re.compile(r"^Entscheidung\b", re.IGNORECASE)


def _is_frozen_snapshot(rel_path: Path) -> bool:
    return any(part.lower() == "archive" or _DATE_DIR_RE.match(part)
               or _DRAFT_BUNDLE_RE.match(part)
               for part in rel_path.parts[:-1])


class DocSection(BaseModel):
    """One heading-delimited section of a docs file. ``heading`` is empty for
    the text before the first heading (a doc's lead-in paragraph, if any)."""

    path: str
    heading: str = ""
    line: int
    text: str


def _split_sections(path: str, full_text: str) -> list[DocSection]:
    """Split a markdown file into sections at each heading line. The text
    before the first heading (if non-empty) becomes its own section with
    ``heading=""`` — dropped entirely only when it is pure whitespace, so a
    file that opens directly with a heading contributes no phantom section.

    Ignores ``#`` lines inside a fenced code block (``` ```/``~~~``) — a
    shell/HTTP example containing a commented-out ``#`` line (W8-Reviewer-
    Fund WARNING-1, Etappe F: measured 1,852 of 15,377 heading-regex hits
    across the real `docs/` tree, 12.0%, coming from inside code fences)
    would otherwise cut a real section in half and attribute the remainder
    to a phantom heading."""
    lines = full_text.splitlines()
    sections: list[DocSection] = []
    cur_heading = ""
    cur_line = 1
    cur_body: list[str] = []
    in_fence = False

    def _flush() -> None:
        text = "\n".join(cur_body).strip()
        if text:
            sections.append(DocSection(path=path, heading=cur_heading,
                                        line=cur_line, text=text))

    for i, ln in enumerate(lines, start=1):
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
            cur_body.append(ln)
            continue
        m = None if in_fence else _HEADING_RE.match(ln)
        if m:
            _flush()
            cur_heading = m.group(2).strip()
            cur_line = i
            cur_body = []
        else:
            cur_body.append(ln)
    _flush()
    return sections


class DocsEngine:
    """Loads `docs/**/*.md` as read-only, searchable sections. Never writes,
    never renders — `docs_recall` returns citations (file + heading + line),
    the caller reads the real file for full context."""

    def __init__(self) -> None:
        self._sections: list[DocSection] = []
        self._root: Path | None = None
        self._bm25_cache: dict | None = None

    @classmethod
    def from_directory(cls, root: str | Path) -> DocsEngine:
        engine = cls()
        engine.load_directory(root)
        return engine

    def load_directory(self, root: str | Path) -> dict:
        """Load (REPLACES any prior load, unlike memory.py/work.py's
        accumulate-across-roots — docs has exactly one root, so "load again"
        means "reload", not "add a second tier"). W8-Reviewer-Fund
        WARNING-3, Etappe F: the first cut appended without clearing, so a
        second `load_directory` call on the same engine double-counted
        every section (`sections` property, banner) — latent because only
        `from_directory` was used until the `_Reloader` wiring, which
        constructs a fresh instance per reload rather than calling this
        twice on one."""
        root_path = Path(root).expanduser()
        if not root_path.exists():
            raise FileNotFoundError(f"Docs directory not found: {root_path}")
        self._root = root_path
        self._sections = []
        self._bm25_cache = None

        added = 0
        skipped_frozen = 0
        for md in iter_files(root_path, ".md"):
            rel = md.relative_to(root_path)
            if _is_frozen_snapshot(rel):
                skipped_frozen += 1
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            for section in _split_sections(str(rel), text):
                self._sections.append(section)
                added += 1
        return {"sections": added, "skipped_frozen_snapshots": skipped_frozen}

    @property
    def sections(self) -> list[DocSection]:
        return list(self._sections)

    def recall(self, query: str, limit: int = 8) -> list[DocSection]:
        """Rank sections by BM25F keyword overlap — same algorithm and floor
        as memory.py/work.py (core.bm25_scores), so one query mechanic serves
        all four domains instead of a fifth independently-tunable copy.

        ``limit`` is clamped to ``[1, _MAX_LIMIT]`` — W8-Reviewer-Fund
        ERROR-1, Etappe F: an LLM-supplied tool parameter is not trusted
        input, and a bare ``scored[:limit]`` treats a negative ``limit`` as
        a valid (reversed, from-the-end) Python slice. Measured against the
        real corpus: ``limit=-1`` returned 1,954 sections (~1.44M tokens)
        for a single tool call — clamping is the whole fix, no other code
        path produces this."""
        bases = bm25_scores(self._bm25_index(), query)
        if not bases:
            return []
        by_key = {self._key(s): s for s in self._sections}
        scored = [(base, by_key[key]) for key, base in bases.items() if base > 0]
        scored.sort(key=lambda s: (-s[0], s[1].path, s[1].line))
        if scored:
            floor = scored[0][0] * _RECALL_FLOOR_RATIO
            scored = [s for s in scored if s[0] >= floor]
        limit = max(1, min(limit, _MAX_LIMIT))
        return [s for _, s in scored[:limit]]

    @staticmethod
    def _key(section: DocSection) -> str:
        # Unique per section (a file has many), stable across reloads as long
        # as the section's own start line does not shift.
        return f"{section.path}::{section.line}"

    def _bm25_index(self) -> dict:
        if self._bm25_cache is None:
            fields_by_key: dict[str, list[tuple[str, float]]] = {
                self._key(s): [
                    (s.heading, _W_HEADING),
                    (s.text, _W_BODY),
                ]
                for s in self._sections
            }
            self._bm25_cache = build_bm25_index(fields_by_key)
        return self._bm25_cache
