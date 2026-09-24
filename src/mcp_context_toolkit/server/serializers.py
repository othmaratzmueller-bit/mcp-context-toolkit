"""Shared output shapes — the same summary dict for a rule/memory/doc
section/work item regardless of which tool returned it, so a client never
has to learn two shapes for one kind of record."""

from __future__ import annotations

from typing import Any

# Snippet length for a docs_recall hit — a CITATION, not the section body.
# An unclamped section can run to tens of thousands of tokens on its own
# (the largest real section measured: ~184,586 chars); this caps a single
# hit's contribution so the response scales with hit count, not section size.
_DOC_SNIPPET_CHARS = 400


def _rule_summary(rule: Any) -> dict:
    return {
        "key": rule.key,
        "short_id": rule.short_id,
        "title": rule.title,
        "type": rule.type,
        "scope": rule.scope,
        "priority": rule.priority,
        "modules": rule.modules,
        "summary": rule.summary,
        "source_path": rule.source_path,
    }


def _rule_full(rule: Any) -> dict:
    return rule.model_dump(mode="json")


def _memory_summary(m: Any) -> dict:
    return {
        "name": m.name,
        "type": m.type,
        "tier": m.tier,
        "description": m.description,
        "source_path": m.source_path,
    }


def _doc_section_summary(s: Any) -> dict:
    text = s.text
    if len(text) > _DOC_SNIPPET_CHARS:
        text = text[:_DOC_SNIPPET_CHARS].rstrip() + "…"
    return {"path": s.path, "heading": s.heading, "line": s.line, "text": text}


def _work_summary(w: Any) -> dict:
    # `reuse` as bool, not content: the plan-keeper must know BEFORE every
    # status change whether a reuse search was recorded at all. Without this
    # field it had to fetch each record individually via get_work — a check
    # that is expensive gets skipped.
    return {"key": w.key, "title": w.title, "kind": w.kind,
            "category": w.category, "status": w.status, "theme": w.theme,
            "part_of": w.part_of, "reuse": w.reuse is not None}
