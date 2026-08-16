"""Memory content-type for the knowledge engine.

Same on-disk shape as rules (a directory of frontmatter files that grows over
time), different query semantics: rules match by file-path glob, memories are
recalled by relevance. Read-only — this engine never writes memory content.
The host agent owns writes; a consolidation pass curates.

Multi-tier from the start: load several roots, each tagged with a tier
("user" = cross-project, "project" = locked to one repo). Load order is
precedence — load the project tier first so it wins on name collision
(specific beats general).
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from mcp_context_toolkit.core import (
    KnowledgeLoadError,
    bm25_scores,
    build_bm25_index,
    iter_files,
    parse_frontmatter,
    prefix_slice,
)
from mcp_context_toolkit.core import LINK_RE as _LINK_RE
from mcp_context_toolkit.core import norm_link as _norm_link

MemoryType = Literal["user", "feedback", "project", "reference", "misc"]
MemoryTier = Literal["core", "user", "project", "unknown"]

_INDEX_REF_RE = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")

# Files that live in a memory dir but are not memory records themselves.
_NON_MEMORY = {"MEMORY.md"}

# Recall field weights — name hit counts most, body least. Used as BM25F field
# multipliers on the term frequency (a hit in the name counts triple). Floats,
# not ints: build_bm25_index's fields_by_key is typed as list[tuple[str,
# float]] (shared across content types, some of which may want fractional
# weights), and a dict value type is invariant in Pyright — ints wouldn't
# widen at the call site even though the runtime arithmetic doesn't care.
_W_NAME, _W_DESC, _W_TAGS, _W_BODY = 3.0, 2.0, 2.0, 1.0

# BM25 parameters, prefix-matching cutoff and the snake_case tokenizer live in
# core.py now — they are content-type-agnostic and work.py needs the exact
# same ranking (see core.py's module docstring for why this was extracted
# rather than left duplicated across two files).

# Relative relevance floor for recall(): a candidate must score at least this
# fraction of the top hit's score to be returned. Drops the long tail of weak
# single-incidental-word matches that would otherwise surface once the strong
# matches are excluded — the per-prompt hook walks DOWN the ranking across a
# same-topic thread (limit + exclude backfill), so without a floor question 4
# would inject near-misses far below the best match. Anchored to the query's
# ABSOLUTE best (computed before any exclude, recomputed per call) — so
# excluding strong hits never lowers the bar for the weak tail. When every
# match is weak (top itself is low) the relative floor keeps them all: the bar
# is "comparatively a near-miss", not an absolute quality gate.
_RECALL_FLOOR_RATIO = 0.25


class Memory(BaseModel):
    name: str
    description: str = ""
    type: MemoryType = "misc"
    tier: MemoryTier = "unknown"
    body: str = ""
    links: list[str] = []           # [[name]] references found in the body
    members: list[str] = []         # for bundled packages: the slugs merged in
    tags: list[str] = []
    resource: str | None = None  # OKF: URI/path of the asset this concept describes
    timestamp: str | None = None  # OKF: ISO 8601 last meaningful change (display/tiebreak only)
    source_path: str | None = None


def _coerce_type(raw) -> MemoryType:
    return raw if raw in ("user", "feedback", "project", "reference") else "misc"


def _meta_get(meta: dict, key: str):
    """Read a frontmatter key top-level, falling back to a nested ``metadata:``
    block. The store carries two coexisting frontmatter styles — flat top-level
    keys and the ``metadata:``-nested convention (bundled packages write
    ``metadata: { type, tier, members }``). Reading BOTH is what keeps
    tier/members/tags/resource/timestamp consistent regardless of style; before
    this, a nested ``members:`` parsed as empty and silently killed the whole
    package member-resolution on such files (only ``type`` had the fallback)."""
    if meta.get(key) is not None:
        return meta[key]
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def _coerce_scalar(v) -> str | None:
    """Normalize an optional scalar frontmatter value to a string. YAML
    auto-parses an unquoted ISO date/datetime into a date/datetime object, so
    coerce those back to ISO 8601 (`.isoformat()`) rather than Python's
    space-separated `str()` form — keeps `timestamp` round-trippable/OKF-shaped."""
    if v is None:
        return None
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return str(v).strip()


def _parse_memory(path: Path, tier: MemoryTier) -> Memory:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise KnowledgeLoadError(path, f"read failed: {e}") from e
    meta, body = parse_frontmatter(text)
    # type/tier/members/tags/resource/timestamp live either top-level or nested
    # under a `metadata:` block — read both (see _meta_get).
    raw_type = _meta_get(meta, "type")
    tags = _meta_get(meta, "tags")
    tags = [str(t) for t in tags] if isinstance(tags, list) else []
    members = _meta_get(meta, "members")
    members = [str(x).strip() for x in members] if isinstance(members, list) else []
    # A frontmatter `tier:` (e.g. a bundled package's core/user/project) overrides
    # the load-directory default; fall back to the load tier otherwise.
    fm_tier = _meta_get(meta, "tier")
    resolved_tier = fm_tier if fm_tier in ("core", "user", "project") else tier
    return Memory(
        name=str(meta.get("name") or path.stem).strip(),
        description=str(meta.get("description") or "").strip(),
        type=_coerce_type(raw_type),
        tier=resolved_tier,
        body=body,
        links=sorted(set(_LINK_RE.findall(body))),
        members=members,
        tags=tags,
        resource=_coerce_scalar(_meta_get(meta, "resource")),
        timestamp=_coerce_scalar(_meta_get(meta, "timestamp")),
        source_path=str(path),
    )


class MemoryEngine:
    """Loads markdown+frontmatter memory files from one or more tiered roots
    and answers recall / get / list / lint. Read-only."""

    def __init__(self) -> None:
        self._memories: list[Memory] = []
        self._roots: list[tuple[Path, MemoryTier]] = []
        self._member_owner_cache: dict[str, str] | None = None
        self._edge_cache: list[tuple[str, str]] | None = None
        self._backlink_cache: dict[str, list[str]] | None = None
        self._bm25_cache: dict | None = None

    @classmethod
    def from_roots(cls, roots: dict[str, str | Path]) -> MemoryEngine:
        """Build from a {tier: path} mapping. Project tier is loaded before
        user tier so project wins on name collision."""
        engine = cls()
        for tier in ("project", "user"):
            if tier in roots:
                engine.load_directory(roots[tier], tier=tier)  # type: ignore[arg-type]
        # any other tiers, deterministic order
        for tier, path in sorted(roots.items()):
            if tier not in ("project", "user"):
                engine.load_directory(path, tier="unknown")
        return engine

    @classmethod
    def from_directory(cls, root: str | Path, tier: MemoryTier = "project") -> MemoryEngine:
        engine = cls()
        engine.load_directory(root, tier=tier)
        return engine

    def load_directory(
        self, root: str | Path, *, tier: MemoryTier = "project", strict: bool = False
    ) -> dict:
        """Load one root and ACCUMULATE (does not replace prior loads).

        On name collision with an already-loaded memory, the new one is skipped
        — so load higher-precedence tiers (project) before lower (user).
        """
        root_path = Path(root).expanduser()
        if not root_path.exists():
            raise FileNotFoundError(f"Memory directory not found: {root_path}")
        self._roots.append((root_path, tier))

        seen = {m.name for m in self._memories}
        added = 0
        skipped = 0
        errors: list[str] = []
        for md in iter_files(root_path, ".md"):
            if md.name in _NON_MEMORY:
                continue
            try:
                mem = _parse_memory(md, tier)
            except KnowledgeLoadError as e:
                if strict:
                    raise
                errors.append(str(e))
                continue
            if mem.name in seen:
                skipped += 1
                continue
            seen.add(mem.name)
            self._memories.append(mem)
            added += 1
        self._member_owner_cache = None  # memory set changed → rebuild lazily
        self._edge_cache = None          # resolved edge list depends on the full set
        self._backlink_cache = None      # inbound-edge map depends on the full set
        self._bm25_cache = None          # corpus statistics change with the set
        return {"added": added, "skipped": skipped, "tier": tier,
                "root": str(root_path), "errors": errors}

    @property
    def memories(self) -> list[Memory]:
        return list(self._memories)

    def _member_owners(self) -> dict[str, str]:
        """Normalized member slug -> owning package name (built once, cached;
        invalidated on every load_directory). Lets a [[member]] link (a
        pre-bundle atomic name) resolve to the package that merged it."""
        if self._member_owner_cache is None:
            owners: dict[str, str] = {}
            for m in self._memories:
                for member in m.members:
                    owners[_norm_link(member)] = m.name
            self._member_owner_cache = owners
        return self._member_owner_cache

    def edges(self) -> list[tuple[str, str]]:
        """The resolved directed link graph as ``(source_name, target_name)``
        pairs — deduped, sorted (built once, cached; invalidated on every
        load_directory alongside the member map).

        Every ``[[link]]`` in a body is resolved exactly the way ``get()``
        follows it: normalized (kebab/snake + .md folded), and a link to a
        bundled member slug is credited to the PACKAGE that absorbed it — so an
        edge never points at a slug ``get()`` could not itself return. A memory
        linking its own absorbed member is dropped (no self-edge). This is the
        one primitive behind both backlinks() and the studio graph view."""
        if self._edge_cache is None:
            name_by_norm = {_norm_link(m.name): m.name for m in self._memories}
            owners = self._member_owners()  # normalized member slug -> package
            seen: set[tuple[str, str]] = set()
            for m in self._memories:
                for link in m.links:
                    norm = _norm_link(link)
                    target = name_by_norm.get(norm) or owners.get(norm)
                    if target and target != m.name:
                        seen.add((m.name, target))
            self._edge_cache = sorted(seen)
        return self._edge_cache

    def _backlinks(self) -> dict[str, list[str]]:
        """Inbound-edge map ``target name -> [source names]`` — the reverse of
        edges(), cached. Self-references already excluded upstream."""
        if self._backlink_cache is None:
            rev: dict[str, set[str]] = {}
            for src, tgt in self.edges():
                rev.setdefault(tgt, set()).add(src)
            self._backlink_cache = {t: sorted(s) for t, s in rev.items()}
        return self._backlink_cache

    def backlinks(self, name: str) -> list[str]:
        """Names of memories whose body links resolve to ``name`` (inbound
        edges), sorted. Empty list when nothing cites it (an orphan-in / leaf)."""
        return self._backlinks().get(name, [])

    def get(self, name: str) -> Memory | None:
        exact = next((m for m in self._memories if m.name == name), None)
        if exact is not None:
            return exact
        # Fall back: a package member resolves to the package that absorbed it,
        # so following a [[old_atomic_slug]] link lands on its bundle.
        owner = self._member_owners().get(_norm_link(name))
        return next((m for m in self._memories if m.name == owner), None) if owner else None

    def list(self, type: MemoryType | None = None,
             tier: MemoryTier | None = None) -> list[Memory]:
        result = self._memories
        if type is not None:
            result = [m for m in result if m.type == type]
        if tier is not None:
            result = [m for m in result if m.tier == tier]
        return sorted(result, key=lambda m: m.name)

    def recall(
        self, query: str, limit: int = 8,
        boost: dict[str, float] | None = None,
        backlink_boost: dict[str, float] | None = None,
    ) -> list[Memory]:
        """Rank memories by keyword overlap with the query across both tiers.

        Deterministic, LLM-free (a consolidation pass can add LLM judgment on top).
        Base score = **BM25F** over name/description/tags/body, where the field
        weights act as term-frequency multipliers (a hit in the name counts
        _W_NAME times).

        BM25F rather than a plain weighted hit count because the earlier scorer
        treated every matched word as equally informative. In a natural-language
        question — which is exactly what a per-prompt hook passes in — that let
        filler words dominate: measured on a real 119-memory store, "der"
        matched 115 of them and still contributed as much as a topic word. IDF
        now prices a term by how rare it is, and length normalisation stops a
        large collector note from matching everything by sheer surface area.

        ``boost`` is the optional frecency (hot/cold) signal: a {name: factor}
        map (see UsageStore.boosts). The final score is
        ``base * (1 + factor)`` so a frequently-used memory is pulled forward —
        warmth multiplies relevance, it does not replace it. The engine stays
        pure-read; the caller (MCP) owns the usage data. With no ``boost`` the
        ordering is identical to before (name tiebreak preserved).

        ``backlink_boost`` is optional: a {name: factor} map whose values are
        added to the score (``base * (1 + heat) + factor``), so structurally
        important knowledge rises in the ranking without needing explicit
        usage. The caller owns the mapping from inbound-link counts to
        factors (the MCP server uses log-dampened ``log1p(count) * 0.1``).
        With no ``backlink_boost`` the ordering is identical to before.

        A relevance floor then drops candidates scoring below
        ``_RECALL_FLOOR_RATIO`` of the top hit, so weak single-word near-misses
        do not surface behind the strong matches (relevant for the per-prompt
        hook's exclude-backfill across a same-topic thread).
        """
        boost = boost or {}
        backlink_boost = backlink_boost or {}

        # Ranking itself (IDF, saturation, length normalisation, snake_case
        # query/index symmetry) lives in core.bm25_scores — shared with
        # work.py so the same reviewed algorithm cannot drift into two
        # independently-buggy copies. What stays here is memory-specific:
        # which fields feed the index, and how frecency/backlink boosts and
        # the relevance floor layer on top of the base score.
        bases = bm25_scores(self._bm25_index(), query)
        if not bases:
            return []

        by_name = {m.name: m for m in self._memories}
        scored: list[tuple[float, float, Memory]] = []
        for name, base in bases.items():
            if base > 0:
                heat = boost.get(name, 0.0)
                # Backlink boost: additive factor from the caller's {name: factor}
                # map (MCP passes log1p(inbound) * 0.1) — small enough not to
                # dominate, enough to lift structurally important memories.
                bl_boost = backlink_boost.get(name, 0.0)
                scored.append((base * (1.0 + heat) + bl_boost, heat, by_name[name]))
        # final score desc, then warmer-on-tie, then name for determinism
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].name))
        # Relevance floor: drop the tail that scores below _RECALL_FLOOR_RATIO of
        # the best hit. Done BEFORE the limit slice (and the caller's exclude),
        # anchored to the absolute top — so the per-prompt hook's exclude-backfill
        # can never walk past it into weak near-misses. See _RECALL_FLOOR_RATIO.
        if scored:
            floor = scored[0][0] * _RECALL_FLOOR_RATIO
            scored = [s for s in scored if s[0] >= floor]
        return [m for _, _, m in scored[:limit]]

    def _bm25_index(self) -> dict:
        """Inverted index for BM25F scoring (built once, cached; invalidated on
        every load_directory alongside the other caches).

        Shape::

            {"postings": {token: {memory_name: weighted_tf}},
             "tokens":   [sorted vocabulary],
             "lengths":  {memory_name: weighted_length},
             "avg_len":  float}

        ``weighted_tf`` already carries the field weights (a term in the name
        counts _W_NAME times), which is what makes this BM25F rather than plain
        BM25 — the field emphasis the ranker had before is preserved, it just
        moves into the term frequency instead of being the whole score.

        Index construction itself is core.build_bm25_index — this method's job
        is only to say WHICH fields feed it and at what weight.
        """
        if self._bm25_cache is None:
            fields_by_key: dict[str, list[tuple[str, float]]] = {
                m.name: [
                    (m.name, _W_NAME),
                    (m.description, _W_DESC),
                    (" ".join(m.tags), _W_TAGS),
                    (m.body, _W_BODY),
                ]
                for m in self._memories
            }
            self._bm25_cache = build_bm25_index(fields_by_key)
        return self._bm25_cache

    @staticmethod
    def _prefix_slice(tokens: list[str], term: str) -> list[str]:
        """Kept as a thin wrapper around core.prefix_slice — a test pins this
        exact call shape, and it is harmless surface to keep stable."""
        return prefix_slice(tokens, term)

    def _owning_root(self, memory: Memory) -> Path | None:
        """The root a memory belongs to for index purposes: the DEEPEST loaded
        root it lives under.

        Membership has to span subdirectories — a store organised in topic
        folders (core/, feedback/, project/) keeps every file in one, and a
        flat ``parent == root`` test reported a constant 0 orphans while real
        ones piled up. But "lives under" alone is not enough either: with
        nested roots (an outer project store, an inner user store) a file would
        belong to BOTH and be flagged as an orphan by the outer index even
        though the inner one lists it correctly. Deepest-match gives each
        memory exactly one owner. Returns None when it lives under no root at
        all (or carries no source path) — such a memory is never an orphan
        candidate, since no index is responsible for it.
        """
        if not memory.source_path:
            return None
        path = Path(memory.source_path)
        owner: Path | None = None
        for root, _ in self._roots:
            if path.is_relative_to(root) and (
                owner is None or len(root.parts) > len(owner.parts)
            ):
                owner = root
        return owner

    def lint(self) -> dict:
        """Read-only hygiene report — the raw material a consolidation pass acts on.

        - broken_links: {memory: [missing [[targets]]]} pointing at no memory.
        - orphans: memory names not referenced anywhere in MEMORY.md.
        - stale_pointers: (file.md) references in MEMORY.md with no such file.
        """
        # A link resolves to any memory name OR any package member slug,
        # compared in normalized form (kebab/snake + .md folded). The reported
        # `missing` keeps the raw link text for the human.
        resolvable = {_norm_link(m.name) for m in self._memories}
        resolvable.update(self._member_owners().keys())  # member slugs (cached, normalized)
        broken = {
            m.name: missing
            for m in self._memories
            if (missing := [link for link in m.links if _norm_link(link) not in resolvable])
        }

        orphans: list[str] = []
        stale: list[str] = []
        for root, _ in self._roots:
            index = root / "MEMORY.md"
            if not index.exists():
                continue
            index_text = index.read_text(encoding="utf-8")
            for m in self._memories:
                if self._owning_root(m) != root:
                    continue
                # source_path is guaranteed here: _owning_root() returns None
                # without one, and None never equals a root.
                fname = Path(str(m.source_path)).name
                if fname not in index_text and m.name not in index_text:
                    orphans.append(m.name)
            for ref in _INDEX_REF_RE.findall(index_text):
                if not (root / ref).exists():
                    stale.append(ref)

        return {
            "total": len(self._memories),
            "tiers": {t: sum(1 for m in self._memories if m.tier == t)
                      for t in {tier for _, tier in self._roots}},
            "broken_links": broken,
            "orphans": sorted(set(orphans)),
            "stale_pointers": sorted(set(stale)),
        }
