"""Capability-search MCP tool — "does this already exist?" (S8/W20 reuse-first)."""

from __future__ import annotations

from mcp_context_toolkit import capability


def register(mcp_tool, wissen) -> None:
    @mcp_tool("capability")
    def find_capability(frage: str, limit: int = 5,
                        art: str | None = None) -> str:
        """Search what this codebase ALREADY does, before writing anything new.

        Call this in the PLAN moment — before the first line of a new
        function, class or module. It answers one question: does this
        capability already exist here?

        **Use the technical terms — the query language does not matter.**
        The index holds identifiers and docstrings. What decides a hit is
        whether the query carries the domain vocabulary (`retry`, `backoff`,
        `queue`, `webhook`, `chunk`, `embed`), not whether the sentence
        around them is written in another language or in English. Measured
        against a real target function: a fluent paraphrase that drops the
        domain terms missed the hit entirely, the same query rewritten with
        the terms kept in (even mid-sentence, even in another language)
        ranked it in the top few, and the terms alone in plain English
        ranked it first.

        Prose in another language with the English technical terms kept in
        is how many real codebases actually get searched, and it works.
        What fails is paraphrasing the vocabulary away. If a query comes
        back weak, do not translate it — put the domain words back in.

        Args:
            frage: what the new code is supposed to DO, in code vocabulary.
            limit: how many candidates to return (default 5; three to five
                are a decision, thirty are a wall of text).
            art: restrict to one kind — "funktion", "baustein" or "plan".
                A "plan" hit means another plan already intends to build
                this; talk to it instead of building beside it.

        Returns a ranked list. Each candidate names WHY it surfaced (a term
        match, or proximity to one along the call/import graph) and carries
        one sentence of description with its source, so the decision needs
        no file to be opened. The result is a SUGGESTION — whether a
        candidate fits is a judgement about meaning, and that judgement is
        yours.
        """
        import json

        index = wissen.current()
        if index is None:
            return json.dumps({
                "fehler": "kein Wissens-Index vorhanden",
                "hinweis": ("erzeugen mit: python3 scripts/wissens_index.py "
                            "(er entsteht sonst automatisch nach jedem Commit)"),
            })
        ergebnis = index.suche(frage, limit=limit, art=art)
        return capability.formatiere(
            ergebnis, capability.drift(index.stand, wissen.projekt))
