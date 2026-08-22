import pytest

from mcp_context_toolkit.core import MAX_RECALL_LIMIT, clamp_limit


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
