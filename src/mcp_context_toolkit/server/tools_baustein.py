"""Baustein/function-doc MCP tools (fifth domain, Work-Record
`mcp_baustein_funktions_doku_domaene_konvergiertes_design`) — DELIBERATELY
separate from `docs` ("Zwei Maschinen, zwei Tool-Saetze — NICHT in ein
Schema pressen"): exact-key lookup over a purpose entry, not ranked search
over prose. Each tool gated on its own JSON file independently — a project
can have baustein docs without function docs or vice versa."""

from __future__ import annotations

import json
from pathlib import Path

from mcp_context_toolkit.host_adapters import discover_host_adapter


class _JsonDocStore:
    """Read-only, exact-key lookup over a flat `{anchor: {...}}` JSON file —
    the SAME file `tools/code-graph/generate_handbuch.py` reads (S8, one
    data source, no second copy to drift out of sync). No BM25, no caching:
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
            `tools/code-graph/data/baustein-docs.json` (the same file the
            TALOS handbuch generator reads — one data source, no drift).

            Call this BEFORE reading an unfamiliar file end to end: if a
            purpose entry exists, it answers "what is this for" in one
            lookup instead of reconstructing it from the code.

            `pfad` is the real repo-relative path (e.g.
            `backend/app/rag/chunking.py`). Returns an empty object `{}`
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
            method. Reads `tools/code-graph/data/function-docs.json`, keyed
            by the SAME anchor format as the handbuch's Ablauf-Anker
            (`pfad.py::Qualname`, e.g.
            `backend/app/rag/chunking.py::ChunkingService.chunk_document`)
            — one anchor convention across the codebase, not a second one
            invented for this domain.

            Populated organically (Work-Record `mcp_baustein_funktions_
            doku_domaene_konvergiertes_design`): the executing agent adds
            an entry for every function it writes or meaningfully changes,
            as part of the same edit — not a batch-generated backfill.
            Returns an empty object `{}` when no entry exists yet."""
            entry = func_store.get(anchor)
            return json.dumps(entry or {}, indent=2, ensure_ascii=False)
