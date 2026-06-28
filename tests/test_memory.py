from pathlib import Path

from mcp_context_toolkit.core import parse_frontmatter
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.usage import UsageStore


def _write(d: Path, name: str, fm: str, body: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


class TestParseFrontmatter:
    def test_no_frontmatter(self):
        meta, body = parse_frontmatter("just body\nmore")
        assert meta == {}
        assert body == "just body\nmore"

    def test_simple(self):
        meta, body = parse_frontmatter("---\nname: x\ntype: feedback\n---\n\nhello")
        assert meta["name"] == "x"
        assert meta["type"] == "feedback"
        assert body == "hello"

    def test_bad_yaml_degrades_not_raises(self):
        meta, body = parse_frontmatter("---\nname: [unclosed\n---\nbody")
        assert meta == {}
        assert "body" in body


class TestMemoryEngine:
    def test_load_skips_memory_md(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback", "alpha content")
        (tmp_path / "MEMORY.md").write_text("# index\n- [a](a.md)\n", encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        assert [m.name for m in e.memories] == ["a"]

    def test_recall_ranks_by_keyword(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: abas_auth\ntype: reference",
               "ABAS booking and authentication details")
        _write(tmp_path, "b.md", "name: chat_render\ntype: project",
               "chat bubble rendering")
        e = MemoryEngine.from_directory(tmp_path)
        hits = e.recall("ABAS authentication", limit=5)
        assert hits[0].name == "abas_auth"

    def test_recall_empty_query_returns_empty(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.recall("   ") == []

    def test_get_and_list_by_type(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback", "x")
        _write(tmp_path, "b.md", "name: b\ntype: project", "y")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").name == "a"
        assert e.get("missing") is None
        assert [m.name for m in e.list(type="feedback")] == ["a"]

    def test_frontmatter_tier_overrides_load_default(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ntier: core", "x")
        _write(tmp_path, "b.md", "name: b\ntype: project", "y")  # no tier → load default
        e = MemoryEngine.from_directory(tmp_path)  # load default = project
        assert e.get("a").tier == "core"      # frontmatter wins
        assert e.get("b").tier == "project"   # falls back to load default

    def test_nested_metadata_type(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\nmetadata:\n  type: reference", "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").type == "reference"

    def test_links_extracted_from_body(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "links to [[b]] and [[c]]")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").links == ["b", "c"]

    def test_tier_precedence_project_wins(self, tmp_path: Path):
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        _write(proj, "dup.md", "name: dup\ntype: project", "PROJECT body")
        _write(user, "dup.md", "name: dup\ntype: feedback", "USER body")
        _write(user, "only_user.md", "name: only_user\ntype: user", "user only")
        e = MemoryEngine.from_roots({"project": proj, "user": user})
        dup = e.get("dup")
        assert dup.tier == "project"
        assert "PROJECT" in dup.body
        assert e.get("only_user").tier == "user"
        assert e.list(tier="user") == [m for m in e.memories if m.name == "only_user"]

    def test_lint_reports_broken_links(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "see [[b]] and [[ghost]]")
        _write(tmp_path, "b.md", "name: b", "hi")
        (tmp_path / "MEMORY.md").write_text("- [a](a.md)\n- [b](b.md)\n", encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        lint = e.lint()
        assert lint["broken_links"]["a"] == ["ghost"]
        assert lint["total"] == 2
        assert lint["orphans"] == []

    def test_lint_reports_orphans_and_stale(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "x")
        (tmp_path / "MEMORY.md").write_text("- [gone](gone.md)\n", encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        lint = e.lint()
        assert "a" in lint["orphans"]            # a.md not in index
        assert "gone.md" in lint["stale_pointers"]  # index points at missing file


class TestUsageStore:
    def test_missing_file_is_empty(self, tmp_path: Path):
        u = UsageStore(tmp_path / "_usage.json")
        assert u.boosts() == {}
        assert u.report() == []

    def test_corrupt_file_degrades_not_raises(self, tmp_path: Path):
        p = tmp_path / "_usage.json"
        p.write_text("{ not json", encoding="utf-8")
        u = UsageStore(p)
        assert u.boosts() == {}

    def test_open_increments_and_persists(self, tmp_path: Path):
        p = tmp_path / "_usage.json"
        u = UsageStore(p)
        u.record_open("a")
        u.record_open("a")
        assert p.exists()
        reloaded = UsageStore(p)               # fresh instance reads from disk
        rows = reloaded.report()
        assert rows[0]["name"] == "a"
        assert rows[0]["opens"] == 2
        assert rows[0]["last_open"] is not None

    def test_report_sorted_hot_first(self, tmp_path: Path):
        u = UsageStore(tmp_path / "_usage.json")
        u.record_open("cold")
        for _ in range(5):
            u.record_open("hot")
        assert [r["name"] for r in u.report()] == ["hot", "cold"]

    def test_open_weighs_three_recalls(self, tmp_path: Path):
        # one open (weight 3) == three recall appearances (weight 1 each)
        u_open = UsageStore(tmp_path / "u_open.json")
        u_open.record_open("x")
        u_recall = UsageStore(tmp_path / "u_recall.json")
        u_recall.record_recall(["y", "y", "y"])
        assert u_open.boosts()["x"] == u_recall.boosts()["y"]

    def test_record_recall_empty_is_noop(self, tmp_path: Path):
        p = tmp_path / "_usage.json"
        u = UsageStore(p)
        u.record_recall([])
        assert u.report() == []
        assert not p.exists()                    # nothing written for empty hit


class TestRecallBoost:
    def test_no_boost_is_backward_compatible(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback", "x y")
        e = MemoryEngine.from_directory(tmp_path)
        assert [m.name for m in e.recall("x y")] == ["a"]

    def test_boost_pulls_warm_memory_forward(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: alpha\ntype: feedback", "shared keyword topic")
        _write(tmp_path, "b.md", "name: beta\ntype: feedback", "shared keyword topic")
        e = MemoryEngine.from_directory(tmp_path)
        # equal keyword score -> deterministic name tiebreak puts alpha first
        assert e.recall("shared keyword topic")[0].name == "alpha"
        # warmth on beta flips the order
        hits = e.recall("shared keyword topic", boost={"beta": 2.0})
        assert hits[0].name == "beta"
