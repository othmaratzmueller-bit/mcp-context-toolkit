"""Memory content-type for the knowledge engine.

Same on-disk shape as rules (a directory of frontmatter files that grows over
time), different query semantics: rules match by file-path glob, memories are
recalled by relevance. Read-only — this engine never writes memory content.
The host agent owns writes; a consolidation pass curates. The one gated
exception lives outside this class: ``recall_learning`` appends replay-proven
bridge terms to a memory's ``tags``.

Multi-tier from the start: load several roots, each tagged with a tier
("user" = cross-project, "project" = locked to one repo). Load order is
precedence — load the project tier first so it wins on name collision
(specific beats general).
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mcp_context_toolkit.usage import RecallEventLog

from mcp_context_toolkit.core import LINK_RE as _LINK_RE
from mcp_context_toolkit.core import (
    MAX_RECALL_LIMIT,
    KnowledgeLoadError,
    bm25_scores,
    build_bm25_index,
    clamp_limit,
    iter_files,
    parse_frontmatter,
    prefix_slice,
)
from mcp_context_toolkit.core import norm_link as _norm_link

MemoryType = Literal["user", "feedback", "project", "reference", "misc"]
MemoryTier = Literal["core", "user", "project", "unknown"]

_INDEX_REF_RE = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")

# Files that live in a memory dir but are not memory records themselves.
_NON_MEMORY = {"MEMORY.md"}

# Recall field weights — name hit counts most, body least. Used as BM25F field
# multipliers on the term frequency (a hit in the name counts triple). Floats,
# because build_bm25_index types a field weight as float (shared across content
# types, some of which want fractional weights).
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


class MemoryContexts(BaseModel):
    """Curated, additive context axes for a memory.

    Context is data for a later, explicit recall lane; it is deliberately not
    indexed as free text. Missing axes mean "no claim", never a mismatch.
    """

    themen: list[str] = Field(default_factory=list)
    taetigkeiten: list[str] = Field(default_factory=list)


class Memory(BaseModel):
    name: str
    description: str = ""
    type: MemoryType = "misc"
    tier: MemoryTier = "unknown"
    body: str = ""
    links: list[str] = []           # [[name]] references found in the body
    members: list[str] = []         # for bundled packages: the slugs merged in
    tags: list[str] = []
    learned_tags: list[str] = []    # written only by recall_learning; ranked like tags, kept
                                    # apart so a re-bundle can rebuild tags from its members
    bausteine: list[str] = []       # optional links to code Bausteine (site_kit.py::path_to_id()
                                    # IDs) — NOT a _bm25_index() field on an atomic memory (see below);
                                    # the bundler DOES carry it into a package's body text today, which
                                    # body is BM25-indexed — harmless only while nothing calls the bundler live
    kontexte: MemoryContexts = Field(default_factory=MemoryContexts)
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
    learned_tags = _meta_get(meta, "learned_tags")
    learned_tags = [str(t) for t in learned_tags] if isinstance(learned_tags, list) else []
    bausteine = _meta_get(meta, "bausteine")
    bausteine = [str(b) for b in bausteine] if isinstance(bausteine, list) else []
    raw_kontexte = _meta_get(meta, "kontexte")
    raw_kontexte = raw_kontexte if isinstance(raw_kontexte, dict) else {}
    kontexte = MemoryContexts(
        themen=[str(value) for value in raw_kontexte.get("themen", [])]
        if isinstance(raw_kontexte.get("themen"), list) else [],
        taetigkeiten=[str(value) for value in raw_kontexte.get("taetigkeiten", [])]
        if isinstance(raw_kontexte.get("taetigkeiten"), list) else [],
    )
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
        learned_tags=learned_tags,
        bausteine=bausteine,
        kontexte=kontexte,
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

    def with_memory(self, replacement: Memory) -> MemoryEngine:
        """A shadow engine: same roots and records, the memory named
        ``replacement.name`` swapped for ``replacement``. Nothing is written
        and this engine's caches stay untouched — the shadow builds its own.
        Recall learning replays a proposed tag change through the real ranking
        path on this copy before a single byte reaches the file."""
        if all(m.name != replacement.name for m in self._memories):
            raise KeyError(replacement.name)
        shadow = MemoryEngine()
        shadow._roots = list(self._roots)
        shadow._memories = [
            replacement if m.name == replacement.name else m for m in self._memories
        ]
        return shadow

    def context_candidates(
        self,
        *,
        bausteine: list[str] | None = None,
        themen: list[str] | None = None,
        taetigkeiten: list[str] | None = None,
    ) -> list[tuple[Memory, dict[str, list[str]]]]:
        """Return curated context matches without changing text recall.

        Each axis is an OR-set. A memory appears once even when it matches
        several values; callers can therefore apply a per-axis maximum rather
        than accidentally rewarding broad memories for their number of links.
        """
        wanted = {
            "bausteine": {value.strip() for value in bausteine or [] if value.strip()},
            "themen": {value.strip() for value in themen or [] if value.strip()},
            "taetigkeiten": {value.strip() for value in taetigkeiten or [] if value.strip()},
        }
        if not any(wanted.values()):
            return []
        matches: list[tuple[Memory, dict[str, list[str]]]] = []
        for memory in self._memories:
            by_axis = {
                "bausteine": sorted(wanted["bausteine"] & set(memory.bausteine)),
                "themen": sorted(wanted["themen"] & set(memory.kontexte.themen)),
                "taetigkeiten": sorted(wanted["taetigkeiten"] & set(memory.kontexte.taetigkeiten)),
            }
            present = {axis: values for axis, values in by_axis.items() if values}
            if present:
                matches.append((memory, present))
        # A concrete, observed module is more specific than a broad activity
        # label.  Axes remain OR alternatives for membership, but their
        # deterministic order must not let alphabetically early activity-only
        # memories consume the bounded two-slot context lane first.
        return sorted(
            matches,
            key=lambda item: (
                "bausteine" not in item[1],
                "themen" not in item[1],
                item[0].name,
            ),
        )

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
        *, max_limit: int = MAX_RECALL_LIMIT,
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

        ``limit`` is clamped via ``core.clamp_limit`` — an LLM-supplied tool
        parameter is not trusted input, and a bare ``scored[:limit]`` treats
        a negative ``limit`` as a valid, reversed Python slice (the same gap
        docs.py's recall() had before it was clamped). ``max_limit`` raises
        that ceiling for a caller with a legitimate reason to ask for more
        than the tool-surface default — the CLI's per-prompt memory recall
        deliberately over-fetches (``limit + len(exclude)``, where
        ``exclude`` is the hook's already-injected set and can grow past 50
        over a long session) so that filtering ``exclude`` back out still
        leaves ``limit`` results; without this the default ceiling would
        silently truncate the candidate pool BEFORE the exclude filter runs,
        under-filling the response with no error.
        """
        kept, _floor, _all_scored = self._recall_scored(
            query, limit, boost, backlink_boost, max_limit=max_limit)
        return [m for _, _, m in kept]

    def _recall_scored(
        self, query: str, limit: int = 8,
        boost: dict[str, float] | None = None,
        backlink_boost: dict[str, float] | None = None,
        *, max_limit: int = MAX_RECALL_LIMIT,
    ) -> tuple[list[tuple[float, float, Memory]], float, list[tuple[float, float, Memory]]]:
        """``recall()``'s ranking, with the intermediate steps exposed for
        observability (Etappe 0 of the "Sumpf+Baum" memory concept,
        `memory_sumpf_und_baum_lebensechtes_recall.md` — an append-only
        per-session event log that records what was OFFERED and at what
        score, without changing the ranking itself). Never used for a
        ranking DECISION outside this class — ``recall()`` stays the only
        public entry point callers rank by.

        Returns ``(kept, floor, all_scored)``:

        - ``kept``: ``(score, heat, memory)`` tuples, post-floor, post-limit
          — exactly what ``recall()`` would return, just not yet stripped
          to bare ``Memory`` objects.
        - ``floor``: the relevance-floor cutoff value used (0.0 if no
          candidate scored above 0, i.e. nothing to floor).
        - ``all_scored``: the full ``(score, heat, memory)`` list, sorted,
          BEFORE the floor cut and BEFORE the limit slice — the honest
          denominator a caller needs to log "how many candidates existed on
          each side of the floor," which cannot be reconstructed from
          ``kept`` alone once ``limit`` has truncated it.
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
            return [], 0.0, []

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
        all_scored = list(scored)
        # Relevance floor: drop the tail that scores below _RECALL_FLOOR_RATIO of
        # the best hit. Done BEFORE the limit slice (and the caller's exclude),
        # anchored to the absolute top — so the per-prompt hook's exclude-backfill
        # can never walk past it into weak near-misses. See _RECALL_FLOOR_RATIO.
        floor = 0.0
        if scored:
            floor = scored[0][0] * _RECALL_FLOOR_RATIO
            scored = [s for s in scored if s[0] >= floor]
        limit = clamp_limit(limit, maximum=max_limit)
        return scored[:limit], floor, all_scored

    def _bm25_index(self) -> dict:
        """Inverted index for BM25F scoring (built once, cached; invalidated on
        every load_directory alongside the other caches).

        Shape::

            {"postings":      {token: {memory_name: weighted_tf}},
             "flat_postings": {token: {memory_name: weighted_tf}},  # tags
             "tokens":        [sorted vocabulary],
             "lengths":       {memory_name: weighted_length},
             "avg_len":       float}

        ``weighted_tf`` already carries the field weights (a term in the name
        counts _W_NAME times), which is what makes this BM25F rather than plain
        BM25 — the field emphasis the ranker had before is preserved, it just
        moves into the term frequency instead of being the whole score.

        Index construction itself is core.build_bm25_index — this method's job
        is only to say WHICH fields feed it and at what weight. ``tags`` is
        the one field that is NOT length-normalised (BM25F per-field b = 0):
        it is a short curated list naming what the record is about — the
        signal recall learning writes — and in a large bundled package the
        body length otherwise dilutes it to nothing.
        """
        if self._bm25_cache is None:
            fields_by_key: dict[str, list[tuple[str, float] | tuple[str, float, bool]]] = {
                m.name: [
                    (m.name, _W_NAME),
                    (m.description, _W_DESC),
                    (" ".join([*m.tags, *m.learned_tags]), _W_TAGS, False),
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
            # Key set from the MEMORIES themselves, not from `self._roots` —
            # a caller that loads a single root under `tier="project"` (the
            # backend KPI wrapper does) still has memories whose OWN `.tier`
            # differs (e.g. a `core`-tagged package folded into that root),
            # and the roots-based key set silently dropped those from the
            # breakdown while `total` still counted them (W8-Reviewer-Fund,
            # mct-Context-Studio-Vollausbau Etappe B: `total: 223` next to
            # `tiers: {project: 198}` on the new Status tile — 25 memories
            # invisible in their own breakdown).
            "tiers": {t: sum(1 for m in self._memories if m.tier == t)
                      for t in {m.tier for m in self._memories}},
            "broken_links": broken,
            "orphans": sorted(set(orphans)),
            "stale_pointers": sorted(set(stale)),
        }


def memory_recall_hits(
    engine: MemoryEngine, query: str, limit: int = 8, usage=None,
    event_log: RecallEventLog | None = None,
    bausteine: list[str] | None = None,
    themen: list[str] | None = None,
    taetigkeiten: list[str] | None = None,
    exclude: set[str] | None = None,
    boosts: dict[str, float] | None = None,
    breadcrumbs: list[Memory] | None = None,
) -> list[Memory]:
    """The real `recall` MCP tool's ranking + side effects, as a plain
    function — pulled out of `server/tools_memory.py`'s `recall()` closure
    (mct-Context-Studio-Vollausbau Etappe E) so a second consumer (a
    BRIAREOS chat tool letting an admin ask the store questions) gets the
    IDENTICAL ranking instead of a reimplementation that silently drifts —
    the S8 lesson from Etappe A generalizes here too: `recall()` is not
    just `engine.recall(query, limit)`, it also computes a backlink boost
    from the memory graph and records frecency usage as a side effect. A
    caller that skipped this and called `engine.recall()` directly would
    rank hits differently (no backlink boost) and never warm up a memory's
    "hot" score, silently diverging from what the MCP tool's own callers
    see for the exact same query.

    Backlink boost: log-dampened, ``log1p(inbound_links) * 0.1`` — a memory
    with 10 inbound links gets ~+0.24 to its score, so structurally
    important knowledge surfaces without explicit usage while the boost
    never dominates the base relevance. `usage` (frecency sidecar) is
    optional — `None` skips both the boost lookup and the recall record.

    `event_log` (Etappe 0, optional — `None` logs nothing, no behaviour
    change) records an `offered` event after the keyword and bounded-context
    lanes have been combined. This keeps source provenance and the context
    candidate denominator available for later evaluation without changing
    this function's return type or keyword ranking at all.

    `breadcrumbs` (optional out-parameter, `None` = collect nothing) — pass
    a list and this function `extend()`s it with the memories that scored
    above the floor but were suppressed by `exclude` (already injected
    earlier in the session), in score order. Unbounded and never cleared by
    this call: a caller reusing the same list across several invocations
    accumulates across all of them, and the exclude/breadcrumbs sets are
    disjoint by construction (`hits` keeps `not in excluded`, breadcrumbs
    keeps `in excluded`) so nothing here can double-count against `hits`.
    Trimming to a caller-facing size (e.g. the CLI's top-3) is the caller's
    job, not this function's."""
    effective_limit = clamp_limit(limit)
    excluded = exclude or set()
    ranking_boosts = usage.boosts() if usage is not None else (boosts or {})
    # ONE engine snapshot for both the graph and the ranking: calling a
    # reloader's current() twice could straddle a reload, scoring hits from
    # one store against link counts from another — not a concern here since
    # the caller already hands us a single `engine`, but the invariant is
    # preserved by construction (one `engine` object, used for both below).
    backlink_boosts: dict[str, float] = {}
    inbound: dict[str, int] = {}
    for _src, tgt in engine.edges():
        inbound[tgt] = inbound.get(tgt, 0) + 1
    for name, count in inbound.items():
        backlink_boosts[name] = math.log1p(count) * 0.1
    keyword_scores: dict[str, float] = {}
    floor_candidates_above = 0
    floor_candidates_below = 0
    # The prompt hook excludes an ever-growing injected set.  Fetch exactly
    # enough extra ranked candidates to fill its requested output *after*
    # filtering; the public response remains clamped to ``effective_limit``.
    # This replaces the hook's former private scorer and keeps that contract
    # in the one shared recall path.
    candidate_limit = effective_limit + len(excluded)
    if event_log is not None or excluded:
        kept, floor, all_scored = engine._recall_scored(
            query, limit=candidate_limit, boost=ranking_boosts,
            backlink_boost=backlink_boosts, max_limit=candidate_limit)
        hits = [m for _, _, m in kept if m.name not in excluded][:effective_limit]
        if breadcrumbs is not None:
            # A suppressed but still relevant hit is not a new injection.  It
            # is a compact reference the caller may render separately, so a
            # long session does not turn the exclude set into silent amnesia.
            breadcrumbs.extend(
                memory for _score, _heat, memory in kept
                if memory.name in excluded
            )
        above = sum(1 for s in all_scored if s[0] >= floor)
        keyword_scores = {
            memory.name: score for score, _hint, memory in kept
            if memory.name not in excluded
        }
        floor_candidates_above = above
        floor_candidates_below = len(all_scored) - above
    else:
        hits = engine.recall(
            query, limit=effective_limit, boost=ranking_boosts, backlink_boost=backlink_boosts,
        )
    # Context is a separate, deliberately narrow candidate lane.  It can fill
    # an otherwise unused part of the response but never displaces a keyword
    # hit, and absent context is byte-for-byte the established recall path.
    context_matches = engine.context_candidates(
        bausteine=bausteine, themen=themen, taetigkeiten=taetigkeiten,
    )
    # An observed module is direct evidence.  When it has curated matches,
    # broad activity/topic alternatives remain visible to explainability but
    # may not consume a bounded delivery slot ahead of (or beside) that exact
    # code context.  If no module match exists, the other OR axes retain their
    # established fallback role.
    delivered_context_matches = context_matches
    if bausteine:
        baustein_matches = [
            item for item in context_matches if "bausteine" in item[1]
        ]
        if baustein_matches:
            delivered_context_matches = baustein_matches
    if delivered_context_matches and len(hits) < effective_limit:
        context_limit = min(2, effective_limit - len(hits))
        known = {memory.name for memory in hits}
        additions = [
            memory for memory, _axes in delivered_context_matches
            if memory.name not in known and memory.name not in excluded
        ][:context_limit]
        hits.extend(additions)
    if event_log is not None:
        # Event scores describe only memories actually offered to the caller.
        # Exclude-overfetch and the output limit may both leave legitimate
        # keyword candidates outside that final set.
        keyword_scores = {
            name: score for name, score in keyword_scores.items()
            if name in {memory.name for memory in hits}
        }
        requested_context = {
            axis: values for axis, values in {
                "bausteine": bausteine or [],
                "themen": themen or [],
                "taetigkeiten": taetigkeiten or [],
            }.items() if values
        }
        matched_context = {memory.name for memory, _axes in context_matches}
        sources = {
            memory.name: (
                (["keyword"] if memory.name in keyword_scores else [])
                + (["context"] if memory.name in matched_context else [])
            )
            for memory in hits
        }
        event_log.log_offered(
            query, [memory.name for memory in hits], keyword_scores,
            floor_candidates_above=floor_candidates_above,
            floor_candidates_below=floor_candidates_below,
            sources=sources,
            context_query=requested_context,
            context_candidates_total=len(context_matches),
            excluded=len(excluded),
        )
    if usage is not None:
        usage.record_recall([m.name for m in hits])
    return hits
