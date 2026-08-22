"""Prose-documentation MCP tools (fourth domain, Work-Record
`doku_lesend_indexieren_statt_zerreissen`)."""

from __future__ import annotations

import json

from .serializers import _doc_section_summary


def register(mcp_tool, docs_reloader) -> None:
    @mcp_tool("docs")
    def docs_recall(query: str, limit: int = 8) -> str:
        """Find sections of prose documentation (docs/**/*.md) by
        keyword, ranked BM25F over heading + body — same ranking
        algorithm as memory/work recall (core.bm25_scores).

        Documentation stays documents here — this does NOT tear docs
        into cards the way the Work-Store does. Each hit is a citation
        (file path + section heading + starting line), not a full
        record: read the file at that path for full context, this only
        tells you WHERE to look. Use this before reading a large doc
        end to end, or when you know roughly what you need but not
        which file it lives in."""
        engine = docs_reloader.current()
        hits = engine.recall(query, limit=limit)
        return json.dumps(
            [_doc_section_summary(s) for s in hits], indent=2, ensure_ascii=False
        )
