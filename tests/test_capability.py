from mcp_context_toolkit.capability import Wissensindex


def _make_index(n_docs: int) -> Wissensindex:
    """Minimal, hand-built BM25 structure — every document shares one term
    at tf=1, so ranking order is irrelevant and only the candidate COUNT
    matters (what these tests exercise)."""
    dokumente = [
        {"id": f"funktion:doc{i}", "titel": f"Doc {i}", "art": "funktion",
         "beschreibung": "x", "pfad": "", "zeile": 0, "quelle": "", "gewicht": 0.0}
        for i in range(n_docs)
    ]
    postings = {"sharedterm": [(i, 1) for i in range(n_docs)]}
    bm25 = {"postings": postings, "laengen": [1] * n_docs, "N": n_docs,
            "avgdl": 1.0, "k1": 1.5, "b": 0.75}
    daten = {"dokumente": dokumente, "bm25": bm25, "stand": "abc123", "kanten": []}
    return Wissensindex(daten, tokenizer=lambda s: s.lower().split())


class TestWissensindexSuche:
    """Etappe 7 of the public-readiness plan: `suche()`'s `limit` now runs
    through `core.clamp_limit` — previously unclamped (`sortiert[:limit]`,
    plus a `weggelassen` count that subtracted `limit` directly, so a
    negative value would have reported MORE omitted candidates than
    actually existed)."""

    def test_negative_limit_clamps_to_one(self):
        index = _make_index(10)
        result = index.suche("sharedterm", limit=-1)
        assert len(result["treffer"]) == 1

    def test_excessive_limit_clamps_to_fifty(self):
        index = _make_index(60)
        result = index.suche("sharedterm", limit=10_000)
        assert len(result["treffer"]) == 50

    def test_weggelassen_count_stays_non_negative_under_the_clamp(self):
        # Before the clamp: weggelassen = max(0, len(sortiert) - limit)
        # with the RAW limit — a negative limit would have inflated this
        # past the real candidate count. After clamping, it reflects what
        # was actually left out of the (now 1-item) result.
        index = _make_index(10)
        result = index.suche("sharedterm", limit=-1)
        assert result["weggelassen"] == 9
