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


class TestPackageTags:
    """Member tags go into the package's own `tags` (the unnormalised recall
    field), not only into the body. `tags` is rebuilt from the current
    members; only `learned_tags` (recall learning) is carried over."""

    def _meta(self, text: str) -> dict:
        from mcp_context_toolkit.core import parse_frontmatter
        return parse_frontmatter(text)[0]

    def test_member_tags_become_package_tags(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ntags: [lügt, 'a: b']", "body a")
        _write(tmp_path, "b.md", "name: b\ntype: feedback\ntags: [Lügt, '#x']", "body b")
        e = MemoryEngine.from_directory(tmp_path)
        pkg = {"file": "feedback/x.md", "title": "X", "members": ["a", "b"]}
        meta = self._meta(render_package(pkg, [e.get("a"), e.get("b")]))
        assert meta["tags"] == ["lügt", "a: b", "#x"]   # deduplicated, YAML-safe

    def test_no_tags_no_field(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback", "body")
        e = MemoryEngine.from_directory(tmp_path)
        meta = self._meta(render_package({"file": "f/x.md", "members": ["a"]}, [e.get("a")]))
        assert "tags" not in meta

    def test_rebundling_keeps_learned_tags_and_drops_departed_member_tags(self, tmp_path: Path):
        # The old package: member "gone" contributed tag "stale"; recall
        # learning wrote "fabricated". The new bundle has member "a" only.
        _write(tmp_path, "x.md", "name: x\ntype: package\nmembers: [gone]\ntags: [stale]\n"
               "learned_tags: [fabricated]", "old")
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ntags: [honesty]\n"
               "learned_tags: [bridged]", "body a")
        out = tmp_path / "_staging"
        write_bundle([{"file": "x.md", "members": ["a"]}], tmp_path, out)
        meta = self._meta((out / "x.md").read_text(encoding="utf-8"))
        assert meta["tags"] == ["honesty"]                        # "stale" left with its member
        assert meta["learned_tags"] == ["fabricated", "bridged"]  # nothing learned is lost

    def test_learned_tags_are_ranked_like_tags(self, tmp_path: Path):
        from mcp_context_toolkit.memory import memory_recall_hits
        _write(tmp_path, "t.md", "name: t\nlearned_tags: [fabricated]", "unrelated words")
        for i in range(5):
            _write(tmp_path, f"n{i}.md", f"name: n{i}", f"note {i} about other things")
        hits = memory_recall_hits(MemoryEngine.from_directory(tmp_path), "fabricated",
                                  limit=3, boosts={})
        assert [m.name for m in hits] == ["t"]

    def test_package_tag_makes_a_large_package_recallable(self, tmp_path: Path):
        # Measured: with the member tag only in the body (the old rendering),
        # a 3000-word package falls below recall's relevance floor entirely;
        # as a package tag it is delivered.
        from mcp_context_toolkit.memory import memory_recall_hits
        _write(tmp_path, "a.md", "name: a\ntype: feedback\ntags: [abschreibt]",
               " ".join(f"filler{i}" for i in range(3000)))
        rendered = render_package({"file": "pkg.md", "members": ["a"]},
                                  [MemoryEngine.from_directory(tmp_path).get("a")])
        body_only = "\n".join(line for line in rendered.splitlines()
                              if not line.startswith("tags: [")) + "\n"

        def recalled(package_text: str, store: Path) -> bool:
            store.mkdir()
            (store / "pkg.md").write_text(package_text, encoding="utf-8")
            for i in range(20):
                _write(store, f"n{i}.md", f"name: n{i}", f"abschreibt note {i} other words")
            hits = memory_recall_hits(MemoryEngine.from_directory(store), "abschreibt",
                                      limit=50, boosts={})
            return "pkg" in [m.name for m in hits]

        assert not recalled(body_only, tmp_path / "old")
        assert recalled(rendered, tmp_path / "new")
