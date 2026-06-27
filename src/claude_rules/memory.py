"""Memory content-type for the knowledge engine.

Same on-disk shape as rules (a directory of frontmatter files that grows over
time), different query semantics: rules match by file-path glob, memories are
recalled by relevance. Read-only — this engine never writes memory. Native
Claude Code writes/auto-loads the project tier; the `/dream` skill curates.

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

from claude_rules.core import KnowledgeLoadError, iter_files, parse_frontmatter

MemoryType = Literal["user", "feedback", "project", "reference", "misc"]
MemoryTier = Literal["user", "project", "unknown"]

_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WORD_RE = re.compile(r"[a-z0-9_]+")
_INDEX_REF_RE = re.compile(r"\(([A-Za-z0-9_./-]+\.md)\)")

# Files that live in a memory dir but are not memory records themselves.
_NON_MEMORY = {"MEMORY.md"}

# Recall field weights — name hit counts most, body least.
_W_NAME, _W_DESC, _W_TAGS, _W_BODY = 3, 2, 2, 1


class Memory(BaseModel):
    name: str
    description: str = ""
    type: MemoryType = "misc"
    tier: MemoryTier = "unknown"
    body: str = ""
    links: list[str] = []           # [[name]] references found in the body
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
    return Memory(
        name=str(meta.get("name") or path.stem).strip(),
        description=str(meta.get("description") or "").strip(),
        type=_coerce_type(raw_type),
        tier=tier,
        body=body,
        links=sorted(set(_LINK_RE.findall(body))),
        tags=tags,
        source_path=str(path),
    )


class MemoryEngine:
    """Loads markdown+frontmatter memory files from one or more tiered roots
    and answers recall / get / list / lint. Read-only."""

    def __init__(self) -> None:
        self._memories: list[Memory] = []
        self._roots: list[tuple[Path, MemoryTier]] = []

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
        return {"added": added, "skipped": skipped, "tier": tier,
                "root": str(root_path), "errors": errors}

    @property
    def memories(self) -> list[Memory]:
        return list(self._memories)

    def get(self, name: str) -> Optional[Memory]:
        return next((m for m in self._memories if m.name == name), None)

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

        Deterministic, LLM-free (the `/dream` skill adds LLM judgment on top).
        Base score = weighted substring hits in name/description/tags/body.

        ``boost`` is the optional frecency (hot/cold) signal: a {name: factor}
        map (see UsageStore.boosts). The final score is
        ``base * (1 + factor)`` so a frequently-used memory is pulled forward —
        warmth multiplies relevance, it does not replace it. The engine stays
        pure-read; the caller (MCP) owns the usage data. With no ``boost`` the
        ordering is identical to before (name tiebreak preserved).
        """
        terms = set(_WORD_RE.findall(query.lower()))
        if not terms:
            return []
        boost = boost or {}
        scored: list[tuple[float, float, Memory]] = []
        for m in self._memories:
            name_l, desc_l = m.name.lower(), m.description.lower()
            tags_l, body_l = " ".join(m.tags).lower(), m.body.lower()
            base = 0
            for t in terms:
                if t in name_l:
                    base += _W_NAME
                if t in desc_l:
                    base += _W_DESC
                if t in tags_l:
                    base += _W_TAGS
                if t in body_l:
                    base += _W_BODY
            if base > 0:
                heat = boost.get(m.name, 0.0)
                scored.append((base * (1.0 + heat), heat, m))
        # final score desc, then warmer-on-tie, then name for determinism
        scored.sort(key=lambda s: (-s[0], -s[1], s[2].name))
        return [m for _, _, m in scored[:limit]]

    def lint(self) -> dict:
        """Read-only hygiene report — the raw material a `/dream` pass acts on.

        - broken_links: {memory: [missing [[targets]]]} pointing at no memory.
        - orphans: memory names not referenced anywhere in MEMORY.md.
        - stale_pointers: (file.md) references in MEMORY.md with no such file.
        """
        names = {m.name for m in self._memories}
        broken = {
            m.name: missing
            for m in self._memories
            if (missing := [l for l in m.links if l not in names])
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
