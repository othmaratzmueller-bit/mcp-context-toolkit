"""Baustein and function-documentation MCP tools.

These exact-key lookups are separate from ranked prose search. Each tool is
gated on its own configured JSON file, so a project can provide either kind
of documentation independently.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp_context_toolkit.host_adapters import discover_host_adapter


class _JsonDocStore:
    """Read-only, exact-key lookup over a flat `{anchor: {...}}` JSON file —
    the same configured source other host tooling may read, with no copied
    index to drift out of sync. No BM25, no caching:
    re-read on every call. Deliberately simple — an agent looks this up
    before touching an unfamiliar file, not in a hot loop, and a fresh read
    is trivially correct where a cache would need its own staleness story."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def get(self, key: str) -> dict | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        entry = data.get(key)
        return entry if isinstance(entry, dict) else None


def register(mcp_tool, baustein_docs_file=None, function_docs_file=None) -> None:
    if baustein_docs_file is not None:
        store = _JsonDocStore(baustein_docs_file)
        path_to_id = discover_host_adapter().path_to_id

        @mcp_tool("baustein")
        def get_baustein_doc(pfad: str) -> str:
            """Fetch the purpose entry for a whole file (a "Baustein") —
            what it does, why it exists, what it is coupled to. Reads
            the configured Baustein documentation JSON file.

            Call this BEFORE reading an unfamiliar file end to end: if a
            purpose entry exists, it answers "what is this for" in one
            lookup instead of reconstructing it from the code.

            `pfad` is the real repo-relative path (e.g.
            `src/project/chunking.py`). Returns an empty object `{}`
            when no entry exists — most files have none, that is the
            normal case, not an error."""
            key = path_to_id(pfad)
            if key is None:
                return json.dumps({}, ensure_ascii=False)
            entry = store.get(key)
            return json.dumps(entry or {}, indent=2, ensure_ascii=False)

    if function_docs_file is not None:
        func_store = _JsonDocStore(function_docs_file)

        @mcp_tool("baustein")
        def get_function_doc(anchor: str) -> str:
            """Fetch the one-sentence purpose entry for a single function or
            method. Reads the configured function-documentation JSON file,
            keyed by `path.py::Qualname` (for example,
            `src/project/chunking.py::ChunkingService.chunk_document`).

            A host may populate this file as functions are written or
            meaningfully changed, rather than as a batch-generated backfill.
            Returns an empty object `{}` when no entry exists yet."""
            entry = func_store.get(anchor)
            return json.dumps(entry or {}, indent=2, ensure_ascii=False)
