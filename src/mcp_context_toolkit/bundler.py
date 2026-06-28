"""Mechanical, lossless bundling of atomic memories into thematic package files.

Takes a bundle map (packages → member names) + the memory store and renders ONE
package file per group by concatenating each member's description + body VERBATIM
under a `## <name>` heading. No summarization, no content loss — a pure
reorganization. The semantic/lossy steps (merging duplicates, compressing bodies,
pruning) are SEPARATE skills (context-compact / context-prune) and gated.

Writes only to an explicit ``out_dir`` (intended: a staging dir) — never the live
store unless the caller passes the live dir deliberately. ``plan_bundle`` is
read-only (coverage check); ``write_bundle`` renders + verifies losslessness:
every member's body must appear verbatim in its package output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from mcp_context_toolkit.memory import Memory, MemoryEngine


def _slug(m: Memory) -> str:
    """Stable identifier = the FILENAME stem. The bundle map keys on slugs, while
    a memory's frontmatter `name:` may be a verbose title (prod-merge drift) — so
    match/render by stem, never by `name`."""
    return Path(m.source_path).stem if m.source_path else m.name


def render_package(pkg: dict, members: list[Memory]) -> str:
    """Render one package file: frontmatter + a `## slug` section per member,
    description italicised, body verbatim. Lossless by construction."""
    name = Path(pkg["file"]).stem
    title = pkg.get("title") or name
    tier = pkg.get("tier", "project")
    head = [
        "---",
        f"name: {name}",
        "type: package",
        f"tier: {tier}",
        # json.dumps → YAML-safe double-quoted scalar (titles contain ': ' e.g.
        # "My Topic: Subtitle" which would otherwise break the frontmatter).
        f"description: {json.dumps(title, ensure_ascii=False)}",
        "members: [" + ", ".join(_slug(m) for m in members) + "]",
        "---",
        "",
        f"# {title}",
        "",
    ]
    parts: list[str] = []
    for m in members:
        parts.append(f"## {_slug(m)}")
        if m.description:
            parts.append(f"*{m.description.strip()}*")
        if m.tags:
            parts.append(f"tags: {', '.join(m.tags)}")
        parts.append("")
        parts.append(m.body.rstrip())
        parts.append("")
    return "\n".join(head + parts).rstrip() + "\n"


def plan_bundle(packages: list[dict], engine: MemoryEngine) -> dict:
    """Read-only coverage check of a bundle map against the store. Returns
    counts + the three failure sets (missing / duplicated / unassigned)."""
    by_slug = {_slug(m): m for m in engine.memories}
    assigned: dict[str, list[str]] = {}
    missing: list[str] = []
    for pkg in packages:
        for mn in pkg["members"]:
            if mn in by_slug:
                assigned.setdefault(mn, []).append(pkg["file"])
            else:
                missing.append(mn)
    duplicated = sorted(mn for mn, files in assigned.items() if len(files) > 1)
    unassigned = sorted(s for s in by_slug if s not in assigned)
    return {
        "packages": len(packages),
        "store_memories": len(engine.memories),
        "assigned": len(assigned),
        "missing": sorted(set(missing)),
        "duplicated": duplicated,
        "unassigned": unassigned,
    }


def write_bundle(
    packages: list[dict],
    memory_dir: str | Path,
    out_dir: str | Path,
    engine: Optional[MemoryEngine] = None,
) -> dict:
    """Render all package files into ``out_dir`` (staging — NOT the live store)
    and verify losslessness. Returns {written, out_dir, lost_bodies}.

    ``lost_bodies`` MUST be empty — it lists members whose verbatim body did not
    survive into its package output (a bug guard, not an expected outcome)."""
    eng = engine or MemoryEngine.from_directory(memory_dir)
    by_slug = {_slug(m): m for m in eng.memories}
    out = Path(out_dir)

    rendered: dict[str, str] = {}
    for pkg in packages:
        members = [by_slug[mn] for mn in pkg["members"] if mn in by_slug]
        rendered[pkg["file"]] = render_package(pkg, members)

    for rel, content in rendered.items():
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    all_out = "\n".join(rendered.values())
    mapped = {mn for pkg in packages for mn in pkg["members"]}
    lost = sorted(
        slug for slug in mapped
        if slug in by_slug and by_slug[slug].body.rstrip()
        and by_slug[slug].body.rstrip() not in all_out
    )
    return {"written": len(rendered), "out_dir": str(out), "lost_bodies": lost}
