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

# Recall field weights — name hit counts most, body least.
_W_NAME, _W_DESC, _W_TAGS, _W_BODY = 3, 2, 2, 1
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
    source_path: Optional[str] = None


def _coerce_type(raw) -> MemoryType:
    return raw if raw in ("user", "feedback", "project", "reference") else "misc"


def _parse_memory(path: Path, tier: MemoryTier) -> Memory:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:  # pragma: no cover - filesystem edge
        raise KnowledgeLoadError(path, f"read failed: {e}") from e
    meta, body = parse_frontmatter(text)
    # type lives either top-level (`type:`) or nested (`metadata: { type: }`).
    raw_type = meta.get("type")
    if raw_type is None and isinstance(meta.get("metadata"), dict):
        raw_type = meta["metadata"].get("type")
    tags = meta.get("tags")
    tags = [str(t) for t in tags] if isinstance(tags, list) else []
    members = meta.get("members")
    members = [str(x).strip() for x in members] if isinstance(members, list) else []
    # A frontmatter `tier:` (e.g. a bundled package's core/user/project) overrides
    # the load-directory default; fall back to the load tier otherwise.
    fm_tier = meta.get("tier")
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
        source_path=str(path),
    )


class MemoryEngine:
    """Loads markdown+frontmatter memory files from one or more tiered roots
    and answers recall / get / list / lint. Read-only."""

    def __init__(self) -> None:
        self._memories: list[Memory] = []
        self._roots: list[tuple[Path, MemoryTier]] = []
        self._member_owner_cache: dict[str, str] | None = None

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
    ) -> list[Memory]:
        """Rank memories by keyword overlap with the query across both tiers.

        Deterministic, LLM-free (a consolidation pass can add LLM judgment on top).
        Base score = weighted substring hits in name/description/tags/body.

        ``boost`` is the optional frecency (hot/cold) signal: a {name: factor}
        map (see UsageStore.boosts). The final score is
        ``base * (1 + factor)`` so a frequently-used memory is pulled forward —
        warmth multiplies relevance, it does not replace it. The engine stays
        pure-read; the caller (MCP) owns the usage data. With no ``boost`` the
        ordering is identical to before (name tiebreak preserved).

        A relevance floor then drops candidates scoring below
        ``_RECALL_FLOOR_RATIO`` of the top hit, so weak single-word near-misses
        do not surface behind the strong matches (relevant for the per-prompt
        hook's exclude-backfill across a same-topic thread).
        """
        terms = set(_WORD_RE.findall(query.lower()))
        if not terms:
            return []
        # Match each term at a word START rather than as a raw substring. The
        # negative-lookbehind `(?<![a-z0-9])` treats letters/digits as "inside a
        # word" but `_`, spaces and punctuation as separators — so a term still
        # follows forward-stems and snake_case parts ("deploy" → "deployment",
        # "workflow" → "deploy_workflow") yet no longer leaks on incidental
        # substrings ("set" ✗ "asset", "api" ✗ "rapid", "in" ✗ "string") that
        # used to flood the ranker. Compiled once per query term.
        patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(t)) for t in terms]
        boost = boost or {}
        scored: list[tuple[float, float, Memory]] = []
        for m in self._memories:
            name_l, desc_l = m.name.lower(), m.description.lower()
            tags_l, body_l = " ".join(m.tags).lower(), m.body.lower()
            base = 0
            for pat in patterns:
                if pat.search(name_l):
                    base += _W_NAME
                if pat.search(desc_l):
                    base += _W_DESC
                if pat.search(tags_l):
                    base += _W_TAGS
                if pat.search(body_l):
                    base += _W_BODY
            if base > 0:
                heat = boost.get(m.name, 0.0)
                scored.append((base * (1.0 + heat), heat, m))
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
            if (missing := [l for l in m.links if _norm_link(l) not in resolvable])
        }

        orphans: list[str] = []
        stale: list[str] = []
        for root, _ in self._roots:
            index = root / "MEMORY.md"
            if not index.exists():
                continue
            index_text = index.read_text(encoding="utf-8")
            for m in self._memories:
                if m.source_path and Path(m.source_path).parent != root:
                    continue
                fname = Path(m.source_path).name if m.source_path else f"{m.name}.md"
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
