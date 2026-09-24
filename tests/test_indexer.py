from pathlib import Path

from mcp_context_toolkit.indexer import (
    DESCRIPTIONS_FILENAME,
    MEMBER_CATALOG_FILENAME,
    build_descriptions,
    build_member_catalog,
    build_memory_md,
    write_descriptions,
    write_member_catalog,
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


class TestMemberCatalogAfterBundling:
    """`build_member_catalog` at the AFTER-bundling moment: the member list comes
    from the bundle map, the description from the package body."""

    def _bundled(self, d: Path) -> None:
        _write(
            d, "pkg.md",
            "name: pkg\ntype: project\ndescription: A package\nmembers: [one, two]",
            "# A package\n\n## one\n*first member*\n\nbody one\n\n"
            "## Status\n\nnot a member, just a heading inside one\n\n"
            "## two\n*second member*\n\nbody two",
        )

    def test_description_read_back_from_package_body(self, tmp_path: Path):
        self._bundled(tmp_path)
        r = write_member_catalog(tmp_path)
        content = (tmp_path / MEMBER_CATALOG_FILENAME).read_text(encoding="utf-8")
        assert r["count"] == 2
        assert "- **one** — first member → pkg.md" in content
        assert "- **two** — second member → pkg.md" in content

    def test_plain_heading_inside_a_body_is_not_a_member(self, tmp_path: Path):
        """The regression this guards: scanning `##` headings cannot tell a member
        apart from a heading inside a member's own body — `## Status` is not a
        memory. The bundle map can."""
        self._bundled(tmp_path)
        content = write_member_catalog(tmp_path) and (
            tmp_path / MEMBER_CATALOG_FILENAME
        ).read_text(encoding="utf-8")
        assert "**Status**" not in content

    def test_heading_with_suffix_still_resolves(self, tmp_path: Path):
        """Sections may carry a suffix (`## slug (2026-07-19)`) — the description
        must still be found, so the slug match is a word boundary, not a line end."""
        _write(
            tmp_path, "pkg.md",
            "name: pkg\ntype: project\ndescription: P\nmembers: [dated_one]",
            "# P\n\n## dated_one (2026-07-19)\n*still described*\n\nbody",
        )
        write_member_catalog(tmp_path)
        content = (tmp_path / MEMBER_CATALOG_FILENAME).read_text(encoding="utf-8")
        assert "- **dated_one** — still described → pkg.md" in content

    def test_unbundled_file_represents_itself(self, tmp_path: Path):
        """A loose memory must stay findable by name too, or bundling would make
        the catalog inconsistent about what it lists."""
        _write(tmp_path, "loose.md", "name: loose\ntype: feedback\ndescription: L", "b")
        assert write_member_catalog(tmp_path)["count"] == 1
        content = (tmp_path / MEMBER_CATALOG_FILENAME).read_text(encoding="utf-8")
        assert "- **loose** — L → loose.md" in content

    def test_member_without_description_still_listed(self, tmp_path: Path):
        _write(
            tmp_path, "pkg.md",
            "name: pkg\ntype: project\ndescription: P\nmembers: [bare]",
            "# P\n\n## bare\n\njust a body",
        )
        write_member_catalog(tmp_path)
        content = (tmp_path / MEMBER_CATALOG_FILENAME).read_text(encoding="utf-8")
        assert "- **bare** —  → pkg.md" in content

    def test_artifact_stays_out_of_the_store(self, tmp_path: Path):
        self._bundled(tmp_path)
        write_member_catalog(tmp_path)
        assert (tmp_path / MEMBER_CATALOG_FILENAME).name.startswith("_")
        assert len(MemoryEngine.from_directory(tmp_path).memories) == 1
