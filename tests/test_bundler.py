from pathlib import Path

from mcp_context_toolkit.bundler import plan_bundle, render_package, write_bundle
from mcp_context_toolkit.memory import MemoryEngine


def _write(d: Path, name: str, fm: str, body: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{fm}\n---\n\n{body}\n", encoding="utf-8")


class TestPlanBundle:
    def test_full_coverage(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ndescription: A", "body A")
        _write(tmp_path, "b.md", "name: b\ntype: feedback\ndescription: B", "body B")
        e = MemoryEngine.from_directory(tmp_path)
        pkgs = [{"file": "feedback/x.md", "title": "X", "tier": "core", "members": ["a", "b"]}]
        p = plan_bundle(pkgs, e)
        assert p["missing"] == [] and p["duplicated"] == [] and p["unassigned"] == []
        assert p["assigned"] == 2

    def test_matches_by_filename_stem_not_verbose_name(self, tmp_path: Path):
        # filename stem 'myslug' but frontmatter name is a verbose title (prod drift)
        _write(tmp_path, "myslug.md", "name: A Verbose Human Title\ntype: feedback\ndescription: D", "body")
        e = MemoryEngine.from_directory(tmp_path)
        pkgs = [{"file": "f/x.md", "title": "X", "tier": "core", "members": ["myslug"]}]
        p = plan_bundle(pkgs, e)
        assert p["missing"] == []       # 'myslug' matches by stem, not by name
        assert p["unassigned"] == []
        assert p["assigned"] == 1

    def test_detects_missing_dup_unassigned(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ndescription: A", "body A")
        _write(tmp_path, "b.md", "name: b\ntype: feedback\ndescription: B", "body B")
        e = MemoryEngine.from_directory(tmp_path)
        pkgs = [{"file": "f/x.md", "members": ["a", "a", "ghost"]}]  # a dup, ghost missing, b unassigned
        p = plan_bundle(pkgs, e)
        assert p["duplicated"] == ["a"]
        assert "ghost" in p["missing"]
        assert "b" in p["unassigned"]


class TestWriteBundle:
    def test_lossless_verbatim_body_and_description(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ndescription: Desc A", "BODY-A-UNIQUE line")
        _write(tmp_path, "b.md", "name: b\ntype: project\ndescription: Desc B", "BODY-B-UNIQUE line")
        e = MemoryEngine.from_directory(tmp_path)
        out = tmp_path / "_staging"
        pkgs = [{"file": "feedback/x.md", "title": "X", "tier": "core", "members": ["a", "b"]}]
        r = write_bundle(pkgs, tmp_path, out, engine=e)
        assert r["lost_bodies"] == []          # nothing lost
        content = (out / "feedback/x.md").read_text(encoding="utf-8")
        assert "BODY-A-UNIQUE line" in content  # body verbatim
        assert "BODY-B-UNIQUE line" in content
        assert "*Desc A*" in content            # description preserved
        assert "## a" in content and "## b" in content
        assert "type: package" in content


class TestRenderPackageYamlSafe:
    def test_colon_title_frontmatter_stays_valid_yaml(self, tmp_path: Path):
        # Regression: titles with ': ' (e.g. "Infra/Hardware: GPU") must not break
        # the package frontmatter — description has to be quoted.
        from mcp_context_toolkit.core import parse_frontmatter
        _write(tmp_path, "x.md", "name: x\ntype: feedback\ndescription: D", "body")
        e = MemoryEngine.from_directory(tmp_path)
        pkg = {"file": "project/infra.md", "title": "Infra/Hardware: GPU, Prod-HW", "tier": "project", "members": ["x"]}
        meta, _ = parse_frontmatter(render_package(pkg, [e.get("x")]))
        assert meta != {}                                        # YAML did NOT break
        assert meta["members"] == ["x"]                          # members readable
        assert meta["description"] == "Infra/Hardware: GPU, Prod-HW"
        assert meta["tier"] == "project"


class TestRenderPackage:
    def test_frontmatter_members(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ndescription: A\ntags: [x, y]", "body")
        e = MemoryEngine.from_directory(tmp_path)
        s = render_package({"file": "feedback/x.md", "title": "X", "tier": "core", "members": ["a"]}, [e.get("a")])
        assert "members: [a]" in s
        assert "name: x" in s and "type: package" in s
        assert "tags: x, y" in s
