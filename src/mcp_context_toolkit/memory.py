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

import bisect
import datetime as _dt
import math
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from mcp_context_toolkit.core import KnowledgeLoadError, iter_files, parse_frontmatter

MemoryType = Literal["user", "feedback", "project", "reference", "misc"]
MemoryTier = Literal["core", "user", "project", "unknown"]

_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WORD_RE = re.compile(r"[a-z0-9_]+")
_INDEX_REF_RE = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")

# Files that live in a memory dir but are not memory records themselves.
_NON_MEMORY = {"MEMORY.md"}

# Recall field weights — name hit counts most, body least. Used as BM25F field
# multipliers on the term frequency (a hit in the name counts triple).
_W_NAME, _W_DESC, _W_TAGS, _W_BODY = 3, 2, 2, 1

# BM25 parameters (Robertson/Sparck-Jones defaults).
#   k1 — term-frequency saturation: the 10th occurrence of a word adds far less
#        than the 2nd, so a long note that merely repeats a term cannot outrank
#        a short one that is actually about it.
#   b  — length normalisation strength (1.0 = full, 0 = off). Needed because a
#        grown store is wildly uneven: here the largest note is ~7x the median,
#        and without normalisation such collector notes match almost any query
#        by sheer surface area.
_BM25_K1 = 1.5
_BM25_B = 0.75
# Shortest query term still resolved by PREFIX (forward-stemming "deploy" ->
# "deployment"). Below it a term must match a vocabulary entry exactly.
# Prefix matching is blind to word boundaries, and on short terms that is
# mostly noise: "wie" reaches "wieder"/"wiederherstellen" (38 entries in a real
# German store), "der" reaches "deren"/"derivable". The useful short matches —
# "pii" -> pii_anonymization, "set" -> set_budget — are snake_case parts, and
# those are indexed as their own tokens anyway, so an exact match still finds
# them. Long terms keep prefix behaviour, where stemming actually pays.
_PREFIX_MIN_LEN = 4
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


def _split_token(token: str) -> list[str]:
    """Split a token into its snake_case parts (or return it unchanged).

    Indexing and querying both go through this, which is what keeps the two
    sides symmetric. Indexing the FULL token alongside its parts would look
    harmless but double-counts: a prefix query for "deploy" matches both the
    part ``deploy`` and the whole ``deploy_workflow``, and pooling their
    postings credits one occurrence twice — and only for tokens whose LEADING
    part matches, so "deploy_workflow" would outscore "release_deploy" on a
    query neither of them mentions more often. Parts only, on both sides.

    The trade-off, deliberately accepted: identifier-exact lookup gets weaker.
    A query for ``deploy_workflow`` now becomes {deploy, workflow} and also
    reaches notes that only mention ``workflow``. The compound is no longer an
    anchor of its own — bag-of-words behaviour, in exchange for a term
    frequency that means what it says.
    """
    if "_" not in token:
        return [token]
    parts = [p for p in token.split("_") if p]
    return parts or [token]


def _norm_link(target: str) -> str:
    """Normalize a [[link]] target or memory name for resolution: drop a
    trailing ``.md`` and fold kebab-case to snake_case. Bundling left links in
    mixed styles (old prod kebab vs dev snake) all pointing at the same slug;
    folding makes them resolve to one canonical form."""
    target = target.strip()
    if target.endswith(".md"):
        target = target[:-3]
    return target.replace("-", "_")


class Memory(BaseModel):
    name: str
    description: str = ""
    type: MemoryType = "misc"
    tier: MemoryTier = "unknown"
    body: str = ""
    links: list[str] = []           # [[name]] references found in the body
    members: list[str] = []         # for bundled packages: the slugs merged in
    tags: list[str] = []
    resource: Optional[str] = None  # OKF: URI/path of the asset this concept describes
    timestamp: Optional[str] = None  # OKF: ISO 8601 last meaningful change (display/tiebreak only)
    source_path: Optional[str] = None


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


def _coerce_scalar(v) -> Optional[str]:
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
    def from_roots(cls, roots: dict[str, str | Path]) -> "MemoryEngine":
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
    def from_directory(cls, root: str | Path, tier: MemoryTier = "project") -> "MemoryEngine":
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

    def get(self, name: str) -> Optional[Memory]:
        exact = next((m for m in self._memories if m.name == name), None)
        if exact is not None:
            return exact
        # Fall back: a package member resolves to the package that absorbed it,
        # so following a [[old_atomic_slug]] link lands on its bundle.
        owner = self._member_owners().get(_norm_link(name))
        return next((m for m in self._memories if m.name == owner), None) if owner else None

    def list(self, type: Optional[MemoryType] = None,
             tier: Optional[MemoryTier] = None) -> list[Memory]:
        result = self._memories
        if type is not None:
            result = [m for m in result if m.type == type]
        if tier is not None:
            result = [m for m in result if m.tier == tier]
        return sorted(result, key=lambda m: m.name)

    def recall(
        self, query: str, limit: int = 8,
        boost: Optional[dict[str, float]] = None,
        backlink_boost: Optional[dict[str, float]] = None,
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
        # Query terms go through the same snake_case split as the index, so a
        # query for "deploy_workflow" resolves against the parts that were
        # actually indexed rather than against a compound that is not there.
        terms = {
            part
            for raw in _WORD_RE.findall(query.lower())
            for part in _split_token(raw)
        }
        if not terms:
            return []
        boost = boost or {}
        backlink_boost = backlink_boost or {}

        index = self._bm25_index()
        postings, vocab = index["postings"], index["tokens"]
        lengths, avg_len = index["lengths"], index["avg_len"]
        total_docs = len(lengths)

        # BM25F: sum over query terms of IDF(term) * saturated, length-normalised
        # term frequency. Each term is resolved to every vocabulary entry it
        # prefixes, and their weighted frequencies are pooled — so "deploy"
        # scores "deployment" and "deploy_workflow" as the same concept.
        bases: dict[str, float] = {}
        for term in terms:
            matches = self._prefix_slice(vocab, term)
            if not matches:
                continue
            # Document frequency of the CONCEPT, not of one spelling: a doc that
            # holds several matching tokens must still be counted once.
            hits: dict[str, float] = {}
            for token in matches:
                for name, tf in postings[token].items():
                    hits[name] = hits.get(name, 0.0) + tf
            # Robertson/Sparck-Jones IDF. A term carried by nearly every memory
            # ("der", "wie") lands near zero, so filler words in a natural-language
            # question stop competing with the words that carry the topic.
            idf = math.log(
                1 + (total_docs - len(hits) + 0.5) / (len(hits) + 0.5)
            )
            for name, tf in hits.items():
                norm = 1 - _BM25_B + _BM25_B * (lengths[name] / avg_len)
                bases[name] = bases.get(name, 0.0) + idf * (
                    tf * (_BM25_K1 + 1) / (tf + _BM25_K1 * norm)
                )

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

        The vocabulary is kept sorted so a query term can be resolved to all
        tokens it PREFIXES via binary search. Prefix semantics are deliberate
        and predate BM25 here: "deploy" must reach "deployment" and the
        snake_case part of "deploy_workflow", while "set" must NOT reach
        "asset". Exact-token BM25 would silently drop the first half of that.
        """
        if self._bm25_cache is None:
            postings: dict[str, dict[str, float]] = {}
            lengths: dict[str, float] = {}
            for m in self._memories:
                fields = (
                    (m.name, _W_NAME),
                    (m.description, _W_DESC),
                    (" ".join(m.tags), _W_TAGS),
                    (m.body, _W_BODY),
                )
                total = 0.0
                for text, weight in fields:
                    for token in _WORD_RE.findall(text.lower()):
                        for part in _split_token(token):
                            postings.setdefault(part, {})
                            postings[part][m.name] = (
                                postings[part].get(m.name, 0.0) + weight
                            )
                            total += weight
                lengths[m.name] = total
            n = len(lengths)
            self._bm25_cache = {
                "postings": postings,
                "tokens": sorted(postings),
                "lengths": lengths,
                "avg_len": (sum(lengths.values()) / n) if n else 0.0,
            }
        return self._bm25_cache

    @staticmethod
    def _prefix_slice(tokens: list[str], term: str) -> list[str]:
        """Vocabulary entries matching ``term``, via binary search.

        Terms shorter than ``_PREFIX_MIN_LEN`` match exactly; longer ones match
        by prefix (forward-stemming). See _PREFIX_MIN_LEN for why.
        """
        lo = bisect.bisect_left(tokens, term)
        if len(term) < _PREFIX_MIN_LEN:
            return [term] if lo < len(tokens) and tokens[lo] == term else []
        # Bound the slice by binary search too, so the work is proportional to
        # the number of MATCHES, not to the vocabulary tail behind them.
        hi = bisect.bisect_right(tokens, term + "￿")
        return tokens[lo:hi]

    def _owning_root(self, memory: Memory) -> Optional[Path]:
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
        owner: Optional[Path] = None
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
