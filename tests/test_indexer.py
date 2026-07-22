from pathlib import Path

from mcp_context_toolkit.indexer import (
    DESCRIPTIONS_FILENAME,
    build_descriptions,
    build_member_catalog,
    build_memory_md,
    write_descriptions,
)
from mcp_context_toolkit.memory import MemoryEngine


def _write(d: Path, name: str, fm: str, body: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


class TestBuildDescriptions:
    def test_one_line_per_memory_sorted(self, tmp_path: Path):
        _write(tmp_path, "b.md", "name: bbb\ntype: project\ndescription: B desc", "x")
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A desc", "y")
        r = build_descriptions(tmp_path)
        assert r["count"] == 2
        entries = [line for line in r["content"].splitlines() if line.startswith("- **")]
        # sorted by (type, name): feedback < project
        assert entries[0].startswith("- **aaa** (feedback)")
        assert entries[1].startswith("- **bbb** (project)")

    def test_line_index_points_at_right_line(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A", "y")
        r = build_descriptions(tmp_path)
        lines = r["content"].split("\n")
        ln = r["line_index"]["aaa"]
        assert lines[ln - 1].startswith("- **aaa**")  # 1-based line number

    def test_skips_memory_md_and_collapses_desc(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A", "y")
        (tmp_path / "MEMORY.md").write_text("# idx\n- [a](a.md)\n", encoding="utf-8")
        r = build_descriptions(tmp_path)
        assert r["count"] == 1  # MEMORY.md not counted as a memory

    def test_deterministic(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A", "y")
        _write(tmp_path, "b.md", "name: bbb\ntype: project\ndescription: B", "x")
        assert build_descriptions(tmp_path)["content"] == build_descriptions(tmp_path)["content"]

    def test_cited_by_suffix_on_linked_memory(self, tmp_path: Path):
        # a.md links to b.md → b's catalog line carries a "← cited by: aaa"
        # suffix; a (nothing cites it) does not. line_index still points right.
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A", "links [[bbb]]")
        _write(tmp_path, "b.md", "name: bbb\ntype: project\ndescription: B", "leaf")
        r = build_descriptions(tmp_path)
        lines = r["content"].split("\n")
        assert lines[r["line_index"]["bbb"] - 1].endswith("← cited by: aaa")
        assert "← cited by" not in lines[r["line_index"]["aaa"] - 1]


class TestBuildMemoryMd:
    def test_lean_toc_by_tier(self):
        pkgs = [
            {"file": "core/a.md", "title": "A", "tier": "core", "members": ["x", "y"]},
            {"file": "project/b.md", "title": "B", "tier": "project", "members": ["z"]},
        ]
        md = build_memory_md(pkgs)
        assert "[A](core/a.md) — 2 memories" in md
        assert "[B](project/b.md) — 1 memories" in md
        assert md.index("## core") < md.index("## project")  # core first


class TestBuildMemberCatalog:
    def test_maps_member_to_package(self, tmp_path: Path):
        _write(tmp_path, "x.md", "name: x\ntype: feedback\ndescription: Desc X", "b")
        e = MemoryEngine.from_directory(tmp_path)
        r = build_member_catalog(
            [{"file": "core/a.md", "title": "A", "tier": "core", "members": ["x"]}], e
        )
        assert r["count"] == 1
        assert "**x** — Desc X → core/a.md" in r["content"]
        assert r["line_index"]["x"] >= 1


class TestWriteDescriptions:
    def test_creates_underscore_artifact_not_reparsed(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: aaa\ntype: feedback\ndescription: A", "y")
        write_descriptions(tmp_path)
        target = tmp_path / DESCRIPTIONS_FILENAME
        assert target.exists()
        assert DESCRIPTIONS_FILENAME.startswith("_")  # excluded from iter_files
        # the generated artifact must NOT be picked up as a memory itself:
        e = MemoryEngine.from_directory(tmp_path)
        assert [m.name for m in e.memories] == ["aaa"]
