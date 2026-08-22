"""Generic Domain Mechanics (Custom Domains & Named Views).

Allows users to configure custom data domains (.context/domains/<id>/domain.yaml)
or named views over existing stores without writing custom Python code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from mcp_context_toolkit.core import KnowledgeLoadError, store_conventions
from mcp_context_toolkit.models import _StrengesModell

DomainProfile = Literal["rules", "memory", "work", "docs"]


class DomainTrigger(_StrengesModell):
    keywords: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class DomainFilter(_StrengesModell):
    # `themes`/`statuses` are the Work vocabulary (WorkItem.theme/.status).
    # `types`/`priorities` are the Rules vocabulary (Rule.type/.priority).
    # A filter only makes sense against the profile it targets; an unrelated
    # pair simply stays empty and has no effect — no cross-profile validation
    # here, same as the rest of this engine leaving shape checks to Pydantic.
    themes: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)


class DomainManifest(_StrengesModell):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str
    root: str | None = None
    profile: DomainProfile = "memory"
    source: DomainProfile | None = None  # None = physical store, non-None = view over source
    read_only: bool = True

    trigger: DomainTrigger = Field(default_factory=DomainTrigger)
    filter: DomainFilter = Field(default_factory=DomainFilter)

    # Custom user-defined metadata attributes (preserves strict validation for core fields)
    attributes: dict = Field(default_factory=dict)
    source_path: str | None = None


def parse_domain_manifest(path: Path) -> DomainManifest:
    """Parse a domain.yaml manifest file into a validated DomainManifest."""
    try:
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            raise KnowledgeLoadError(path, "Manifest must be a YAML dictionary")
        manifest = DomainManifest(**raw, source_path=str(path))
        return manifest
    except Exception as e:
        if isinstance(e, KnowledgeLoadError):
            raise
        raise KnowledgeLoadError(path, f"Failed to parse domain manifest: {e}") from e


class DomainEngine:
    """Manages custom domains and named views across workspace roots."""

    def __init__(self) -> None:
        self._manifests: dict[str, DomainManifest] = {}

    @classmethod
    def from_directory(cls, domains_dir: Path | str) -> DomainEngine:
        """Load every ``<domains_dir>/<id>/domain.yaml`` directly, no walk-up.
        Same `.from_directory()` shape as `WorkEngine`/`MemoryEngine`/
        `DocsEngine` — for a caller that already resolved the directory
        (e.g. the MCP server via `core.discover_domains_dir()`)."""
        engine = cls()
        for manifest_file in Path(domains_dir).glob("*/domain.yaml"):
            try:
                manifest = parse_domain_manifest(manifest_file)
                engine._manifests[manifest.id] = manifest
            except KnowledgeLoadError:
                continue
        return engine

    @classmethod
    def discover_domains(cls, root: Path | None = None) -> DomainEngine:
        engine = cls()
        base_dir = root or Path.cwd()
        candidates = [base_dir, *base_dir.parents]
        for cand in candidates:
            for conv in store_conventions():
                dom_dir = cand / conv / "domains"
                if dom_dir.is_dir():
                    for manifest_file in dom_dir.glob("*/domain.yaml"):
                        try:
                            manifest = parse_domain_manifest(manifest_file)
                            engine._manifests[manifest.id] = manifest
                        except KnowledgeLoadError:
                            continue
        return engine

    def register_manifest(self, manifest: DomainManifest) -> None:
        self._manifests[manifest.id] = manifest

    def list_domains(self) -> list[dict]:
        return [
            {
                "id": m.id,
                "title": m.title,
                "profile": m.profile,
                "source": m.source,
                "is_view": m.source is not None,
                "keywords": m.trigger.keywords,
                "paths": m.trigger.paths,
            }
            for m in self._manifests.values()
        ]

    def get_domain(self, domain_id: str) -> DomainManifest | None:
        return self._manifests.get(domain_id)
