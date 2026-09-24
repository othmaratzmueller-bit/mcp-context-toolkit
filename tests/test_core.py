import math
from pathlib import Path

import pytest

from mcp_context_toolkit.core import (
    BM25_B,
    BM25_K1,
    MAX_RECALL_LIMIT,
    bm25_scores,
    build_bm25_index,
    clamp_limit,
    discover_docs_dir,
    discover_work_dir,
    prefix_slice,
    query_terms,
)


class TestUnicodeTokenizer:
    """German words used to be indexed as fragments: the ASCII class split
    "lügt" into {l, gt}, so a query for it matched anything with a lone "l"."""

    def test_umlaut_words_stay_whole(self):
        assert query_terms("warum lügt ein Test, der prüft?") >= {"lügt", "prüft"}
        assert "gt" not in query_terms("lügt")

    def test_fragments_no_longer_match_the_word(self):
        index = build_bm25_index({
            "real": [("der test lügt", 1.0)],
            "fragments": [("l gt irgendwas", 1.0)],
        })
        assert set(bm25_scores(index, "lügt")) == {"real"}

    def test_decomposed_text_yields_the_same_terms(self):
        import unicodedata
        nfd = unicodedata.normalize("NFD", "lügt prüft")
        assert nfd != "lügt prüft"
        assert query_terms(nfd) == query_terms("lügt prüft") == {"lügt", "prüft"}

    def test_decomposed_index_text_matches_composed_query(self):
        import unicodedata
        index = build_bm25_index({"a": [(unicodedata.normalize("NFD", "der test lügt"), 1.0)]})
        assert set(bm25_scores(index, "lügt")) == {"a"}

    def test_prefix_reaches_umlaut_continuations(self):
        vocab = sorted(["prüfen", "prüfung", "prüfstand", "prost"])
        assert prefix_slice(vocab, "prüf") == ["prüfen", "prüfstand", "prüfung"]

    def test_prefix_reaches_continuations_beyond_the_bmp(self):
        # U+1D51E (a letter, so \w) sorts above U+FFFD — the old sentinel
        # cut the slice before it.
        vocab = sorted(["abcd", "abcd\U0001d51e", "abce"])
        assert prefix_slice(vocab, "abcd") == ["abcd", "abcd\U0001d51e"]


class TestUnnormalisedField:
    """BM25F per-field b = 0 for a field passed with length_normalised=False
    (memory tags): its hits do not shrink with the record's length."""

    def _long_body(self, n: int) -> str:
        return " ".join(f"filler{i}" for i in range(n))

    def test_flat_field_does_not_add_to_length(self):
        index = build_bm25_index({"a": [("x y z", 1.0), ("tag1 tag2", 2.0, False)]})
        assert index["lengths"]["a"] == 3.0
        assert index["flat_postings"]["tag1"] == {"a": 2.0}

    def test_records_without_flat_hits_score_exactly_as_before(self):
        # The original single-expression formula, computed by hand.
        index = build_bm25_index({
            "a": [("alpha beta", 1.0)], "b": [("alpha " + self._long_body(20), 1.0)],
            "c": [("gamma", 1.0), ("alpha", 2.0, False)],
        })
        scores = bm25_scores(index, "alpha")
        df, n, avg = 3, 3, index["avg_len"]
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for key in ("a", "b"):
            norm = 1 - BM25_B + BM25_B * index["lengths"][key] / avg
            assert scores[key] == idf * (1.0 * (BM25_K1 + 1) / (1.0 + BM25_K1 * norm))

    def test_result_order_follows_the_postings_not_the_hash_seed(self):
        # tools_domains sorts by score alone; on a tie, this order decides.
        keys = [f"r{i}" for i in range(8)]
        index = build_bm25_index({k: [("same words", 1.0)] for k in keys})
        assert list(bm25_scores(index, "same")) == keys

    def test_tag_on_a_huge_record_is_not_diluted(self):
        def ratio_with(flat: bool) -> float:
            tag = ("fabricated", 2.0, False) if flat else ("fabricated", 2.0)
            records = {
                "package": [(self._long_body(3000), 1.0), tag],
                "small": [("fabricated once here", 1.0)],
            }
            # A realistic store: many short records keep the average length
            # low, which is what makes one huge package "long".
            records.update({f"note{i}": [(f"note {i} about other things", 1.0)]
                            for i in range(30)})
            scores = bm25_scores(build_bm25_index(records), "fabricated")
            return scores["package"] / scores["small"]

        # Measured: normalised 0.078, flat 0.805 of a short record's score.
        assert ratio_with(flat=False) < 0.2
        assert ratio_with(flat=True) > 0.5


class TestClampLimit:
    """Etappe 7 of the public-readiness plan: one shared clamp for every
    `limit:` parameter across rules/memory/work/docs, instead of a
    per-call-site `[:limit]` slice that treats a negative value as a
    valid, reversed Python slice."""

    def test_valid_value_passes_through_unchanged(self):
        assert clamp_limit(8) == 8
        assert clamp_limit(1) == 1
        assert clamp_limit(MAX_RECALL_LIMIT) == MAX_RECALL_LIMIT

    @pytest.mark.parametrize("bad", [-1, -999, 0])
    def test_zero_or_negative_clamps_to_one(self, bad):
        assert clamp_limit(bad) == 1

    def test_excessive_value_clamps_to_maximum(self):
        assert clamp_limit(999_999) == MAX_RECALL_LIMIT

    def test_custom_maximum_is_respected(self):
        # A caller with a legitimate reason to exceed the tool-surface
        # default (e.g. cli.py's per-prompt memory recall, which
        # deliberately over-fetches past 50 once a session's growing
        # exclude set demands it — see MemoryEngine.recall()'s max_limit)
        # must still get its own ceiling enforced, not silently widened
        # or narrowed by the shared default.
        assert clamp_limit(999, maximum=5) == 5
        assert clamp_limit(-1, maximum=5) == 1

    def test_string_digit_is_coerced_since_python_int_accepts_it(self):
        # Framework-level type validation (FastMCP) rejects a non-numeric
        # string before this function ever runs — but a numeric string is
        # legal input to Python's int(), so this documents that boundary
        # rather than assuming it.
        assert clamp_limit("8") == 8


class TestDiscoverDocsDir:
    """Promoted from server/discovery.py (mct-Context-Studio-Vollausbau,
    Etappe A) so the CLI's --export-studio can use it too — see the
    function's own docstring for why the walk-up loop is inlined rather
    than calling host_adapters._find_project_root (that would be a
    circular import back into this module)."""

    def test_env_var_wins_even_without_a_marker_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_DOCS_DIR", raising=False)
        docs = tmp_path / "somewhere" / "docs"
        docs.mkdir(parents=True)
        monkeypatch.setenv("CONTEXT_DOCS_DIR", str(docs))
        assert discover_docs_dir() == docs

    def test_env_var_pointing_at_a_missing_dir_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CONTEXT_DOCS_DIR", str(tmp_path / "nope"))
        assert discover_docs_dir() is None

    def test_walks_up_for_a_docs_sibling_of_the_marker_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_DOCS_DIR", raising=False)
        (tmp_path / ".context").mkdir()
        (tmp_path / "docs").mkdir()
        nested = tmp_path / "src" / "deep"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        assert discover_docs_dir() == tmp_path / "docs"

    def test_marker_dir_without_a_docs_sibling_returns_none(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_DOCS_DIR", raising=False)
        (tmp_path / ".context").mkdir()
        monkeypatch.chdir(tmp_path)
        assert discover_docs_dir() is None

    def test_no_marker_dir_anywhere_up_returns_none(self, tmp_path: Path, monkeypatch):
        # pytest's tmp_path lives under the system temp root, which carries
        # no `.claude`/`.context` marker in its ancestry — the walk-up
        # genuinely exhausts every candidate, so this is a real "not found",
        # not a hedged "maybe" assertion.
        monkeypatch.delenv("CONTEXT_DOCS_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        assert discover_docs_dir() is None

    def test_env_only_skips_the_walkup_fallback(self, tmp_path: Path, monkeypatch):
        # Regression coverage for the TALOS finding (2026-08-22): omitting
        # CONTEXT_DOCS_DIR was meant to opt a caller OUT of docs discovery,
        # but --export-studio still found docs/ via the cwd walk-up anyway —
        # env_only=True is the fix, this proves the walk-up is genuinely
        # skipped rather than just unlikely to trigger in this test's cwd.
        monkeypatch.delenv("CONTEXT_DOCS_DIR", raising=False)
        (tmp_path / ".context").mkdir()
        (tmp_path / "docs").mkdir()
        monkeypatch.chdir(tmp_path)
        assert discover_docs_dir() == tmp_path / "docs"  # walk-up still works by default
        assert discover_docs_dir(env_only=True) is None  # but not with env_only

    def test_env_only_still_honors_an_explicit_env_var(self, tmp_path: Path, monkeypatch):
        docs = tmp_path / "somewhere" / "docs"
        docs.mkdir(parents=True)
        monkeypatch.setenv("CONTEXT_DOCS_DIR", str(docs))
        assert discover_docs_dir(env_only=True) == docs


class TestDiscoverWorkDir:
    """`env_only` mirrors `discover_docs_dir`'s — same TALOS finding, same
    fix, same caller (`cli.py`'s `--export-studio` wiring)."""

    def test_env_var_wins_even_without_a_marker_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_WORK_DIR", raising=False)
        work = tmp_path / "somewhere" / "work"
        work.mkdir(parents=True)
        monkeypatch.setenv("CONTEXT_WORK_DIR", str(work))
        assert discover_work_dir() == work.resolve()

    def test_walks_up_for_a_work_sibling_of_the_marker_dir(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_WORK_DIR", raising=False)
        (tmp_path / ".context").mkdir()
        (tmp_path / ".context" / "work").mkdir()
        monkeypatch.chdir(tmp_path)
        assert discover_work_dir() == (tmp_path / ".context" / "work").resolve()

    def test_env_only_skips_the_walkup_fallback(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("CONTEXT_WORK_DIR", raising=False)
        (tmp_path / ".context").mkdir()
        (tmp_path / ".context" / "work").mkdir()
        monkeypatch.chdir(tmp_path)
        assert discover_work_dir() is not None  # walk-up still works by default
        assert discover_work_dir(env_only=True) is None  # but not with env_only

    def test_env_only_still_honors_an_explicit_env_var(self, tmp_path: Path, monkeypatch):
        work = tmp_path / "somewhere" / "work"
        work.mkdir(parents=True)
        monkeypatch.setenv("CONTEXT_WORK_DIR", str(work))
        assert discover_work_dir(env_only=True) == work.resolve()
