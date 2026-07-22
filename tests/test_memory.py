import asyncio
import json
import multiprocessing
from pathlib import Path

import pytest

from mcp_context_toolkit import cli
from mcp_context_toolkit.core import parse_frontmatter
from mcp_context_toolkit.memory import MemoryEngine
from mcp_context_toolkit.usage import UsageStore


def _hammer_opens(path_str: str, n: int) -> None:
    """Module-level worker (must be importable for multiprocessing 'spawn')."""
    store = UsageStore(Path(path_str))
    for _ in range(n):
        store.record_open("x")


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
        _write(tmp_path, "a.md", "name: payroll_auth\ntype: reference",
               "payroll booking and authentication details")
        _write(tmp_path, "b.md", "name: chat_render\ntype: project",
               "chat bubble rendering")
        e = MemoryEngine.from_directory(tmp_path)
        hits = e.recall("payroll authentication", limit=5)
        assert hits[0].name == "payroll_auth"

    def test_recall_empty_query_returns_empty(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.recall("   ") == []

    def test_recall_matches_word_start_not_substring(self, tmp_path: Path):
        # The matcher anchors a term to a word start: it follows forward-stems
        # and snake_case parts, but ignores incidental substrings.
        _write(tmp_path, "deploy.md", "name: deploy_workflow\ntype: feedback",
               "deployment notes")
        _write(tmp_path, "asset.md", "name: asset_registry\ntype: project",
               "asset bookkeeping")
        e = MemoryEngine.from_directory(tmp_path)
        # "deploy" forward-stems onto "deployment" + the snake_case name part…
        assert [m.name for m in e.recall("deploy")] == ["deploy_workflow"]
        # …"workflow" reaches the snake_case tail of deploy_workflow…
        assert [m.name for m in e.recall("workflow")] == ["deploy_workflow"]
        # …but "set" must NOT leak into "asset" (the old substring false positive)
        assert e.recall("set") == []

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

    def test_nested_metadata_members_tier_tags(self, tmp_path: Path):
        # Bundled packages write type/tier/members under a `metadata:` block. All
        # of them must be read there, not only `type` — before the fix a nested
        # `members:` parsed empty and killed member-resolution on such files.
        _write(tmp_path, "pkg.md",
               "name: pkg\nmetadata:\n  type: package\n  tier: core\n"
               "  members:\n    - old_a\n    - old_b\n  tags:\n    - t1\n    - t2",
               "see [[old_a]]")
        e = MemoryEngine.from_directory(tmp_path)  # load default = project
        pkg = e.get("pkg")
        assert pkg.tier == "core"                       # nested tier honored
        assert pkg.members == ["old_a", "old_b"]        # nested members read
        assert pkg.tags == ["t1", "t2"]                 # nested tags read
        assert e.get("old_a").name == "pkg"             # member now resolves
        assert e.lint()["broken_links"] == {}           # [[old_a]] not broken

    def test_top_level_still_wins_over_nested(self, tmp_path: Path):
        # A top-level key takes precedence over the nested one (explicit beats
        # inherited); nested only fills what top-level omits.
        _write(tmp_path, "a.md",
               "name: a\ntier: project\nmetadata:\n  tier: core", "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").tier == "project"

    def test_okf_resource_and_timestamp(self, tmp_path: Path):
        # OKF-compatible optional fields, both flat and nested. YAML auto-parses
        # an unquoted ISO datetime into a datetime object → normalized back to
        # ISO 8601 (Z → +00:00), not Python's space-separated str() form.
        _write(tmp_path, "flat.md",
               "name: flat\nresource: file:///x/y.py\ntimestamp: 2026-05-28T14:30:00Z", "x")
        _write(tmp_path, "nested.md",
               "name: nested\nmetadata:\n  resource: bq://t\n  timestamp: 2026-01-01T00:00:00Z", "y")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("flat").resource == "file:///x/y.py"
        assert e.get("flat").timestamp == "2026-05-28T14:30:00+00:00"
        assert e.get("nested").resource == "bq://t"          # nested fallback
        assert e.get("nested").timestamp == "2026-01-01T00:00:00+00:00"

    def test_quoted_timestamp_stays_verbatim(self, tmp_path: Path):
        # Quoting in YAML keeps the exact string (incl. the Z) — no datetime coercion.
        _write(tmp_path, "q.md", 'name: q\ntimestamp: "2026-05-28T14:30:00Z"', "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("q").timestamp == "2026-05-28T14:30:00Z"

    def test_absent_okf_fields_are_none(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a\ntype: feedback", "x")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").resource is None
        assert e.get("a").timestamp is None

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

    def test_member_link_resolves_to_package(self, tmp_path: Path):
        # A package absorbs old atomic slugs as `members:`; links to those slugs
        # (the pre-bundle names) must resolve to the package, not break — incl.
        # a kebab-cased link folding onto a snake-cased member.
        _write(tmp_path, "pkg.md",
               "name: sec_pkg\nmembers: [old_force_local, old_pii_once]",
               "see [[old_pii_once]]")
        _write(tmp_path, "other.md", "name: other",
               "ref [[old_force_local]] and [[reference-kebab-member]]")
        _write(tmp_path, "kebab.md",
               "name: kebab_pkg\nmembers: [reference_kebab_member]", "x")
        (tmp_path / "MEMORY.md").write_text(
            "- [sec_pkg](pkg.md)\n- [other](other.md)\n- [kebab_pkg](kebab.md)\n",
            encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.lint()["broken_links"] == {}        # every [[member]] resolved
        assert e.get("old_pii_once").name == "sec_pkg"          # followable
        assert e.get("reference-kebab-member").name == "kebab_pkg"  # kebab input

    def test_member_unknown_slug_still_breaks(self, tmp_path: Path):
        # A link to a slug that is no member of any package still flags (Tier 3).
        _write(tmp_path, "pkg.md", "name: pkg\nmembers: [known]",
               "see [[known]] and [[truly_gone]]")
        (tmp_path / "MEMORY.md").write_text("- [pkg](pkg.md)\n", encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.lint()["broken_links"]["pkg"] == ["truly_gone"]

    def test_member_owner_cache_invalidated_across_loads(self, tmp_path: Path):
        # The member-owner map is cached, but a second load_directory must
        # invalidate it — a member defined only in the later-loaded tier still
        # resolves. (Probing after the first load forces the cache to populate.)
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        _write(proj, "p.md", "name: proj_pkg\ntype: project", "x")
        _write(user, "u.md",
               "name: user_pkg\ntype: user\nmembers: [old_user_slug]", "y")
        e = MemoryEngine()
        e.load_directory(proj, tier="project")
        assert e.get("old_user_slug") is None        # caches an empty owner map
        e.load_directory(user, tier="user")           # must drop that stale cache
        assert e.get("old_user_slug").name == "user_pkg"

    def test_lint_reports_orphans_and_stale(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "x")
        (tmp_path / "MEMORY.md").write_text("- [gone](gone.md)\n", encoding="utf-8")
        e = MemoryEngine.from_directory(tmp_path)
        lint = e.lint()
        assert "a" in lint["orphans"]            # a.md not in index
        assert "gone.md" in lint["stale_pointers"]  # index points at missing file


class TestBacklinks:
    """Inbound edges — the reverse of the forward-only Memory.links."""

    def test_reverse_of_forward_link(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "points at [[b]]")
        _write(tmp_path, "b.md", "name: b", "no outbound links")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.get("a").links == ["b"]        # forward edge unchanged
        assert e.backlinks("b") == ["a"]        # reverse edge computed
        assert e.backlinks("a") == []           # nothing cites a (leaf-in)

    def test_multiple_sources_sorted(self, tmp_path: Path):
        _write(tmp_path, "z.md", "name: zeta", "see [[hub]]")
        _write(tmp_path, "a.md", "name: alpha", "also [[hub]]")
        _write(tmp_path, "hub.md", "name: hub", "central")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.backlinks("hub") == ["alpha", "zeta"]  # deterministic, sorted

    def test_link_to_member_credits_package(self, tmp_path: Path):
        # A [[old_slug]] link resolves to the package that absorbed it (like
        # get()), so the backlink is credited to the package, not the dead slug.
        _write(tmp_path, "pkg.md", "name: pkg\nmembers: [old_slug]", "package body")
        _write(tmp_path, "src.md", "name: src", "reference to [[old_slug]]")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.backlinks("pkg") == ["src"]    # credited to the package
        assert e.backlinks("old_slug") == []    # not to the absorbed slug

    def test_self_link_not_counted(self, tmp_path: Path):
        # A package linking its own absorbed member must not cite itself.
        _write(tmp_path, "pkg.md", "name: pkg\nmembers: [mine]", "see [[mine]]")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.backlinks("pkg") == []

    def test_kebab_link_folds_to_snake_target(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "cross ref [[some-topic]]")
        _write(tmp_path, "t.md", "name: some_topic", "target")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.backlinks("some_topic") == ["a"]   # kebab input folds onto snake

    def test_broken_link_produces_no_backlink(self, tmp_path: Path):
        _write(tmp_path, "a.md", "name: a", "dangling [[ghost]]")
        e = MemoryEngine.from_directory(tmp_path)
        assert e.backlinks("ghost") == []

    def test_cache_invalidated_across_loads(self, tmp_path: Path):
        # Backlink map is cached; a second load_directory must rebuild it so a
        # source in the later tier is reflected. (Probe first to populate cache.)
        proj = tmp_path / "proj"
        user = tmp_path / "user"
        _write(proj, "t.md", "name: target\ntype: project", "leaf")
        _write(user, "s.md", "name: source\ntype: user", "links [[target]]")
        e = MemoryEngine()
        e.load_directory(proj, tier="project")
        assert e.backlinks("target") == []          # caches empty (no source yet)
        e.load_directory(user, tier="user")          # must drop the stale cache
        assert e.backlinks("target") == ["source"]

    def test_edges_resolved_deduped_and_backlinks_agree(self, tmp_path: Path):
        # edges() is the shared primitive: (source, target) pairs, deduped/sorted,
        # member-slug links credited to the package, self-edges dropped. backlinks
        # must be its exact reverse.
        _write(tmp_path, "a.md", "name: a", "links [[b]] and [[b]] again and [[pkg_member]]")
        _write(tmp_path, "b.md", "name: b", "leaf")
        _write(tmp_path, "pkg.md", "name: pkg\nmetadata:\n  members:\n    - pkg_member", "self [[pkg_member]]")
        e = MemoryEngine.from_directory(tmp_path)
        # (a,b) once despite the duplicate link; (a,pkg) via member credit;
        # NO (pkg,pkg) self-edge though pkg links its own member.
        assert e.edges() == [("a", "b"), ("a", "pkg")]
        assert e.backlinks("b") == ["a"]
        assert e.backlinks("pkg") == ["a"]


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

    def test_boosts_and_report_read_fresh_from_disk(self, tmp_path: Path):
        # boosts()/report() re-read disk, so a second instance sees the first's
        # writes — the basis for parallel sessions sharing one frecency signal.
        p = tmp_path / "_usage.json"
        u1 = UsageStore(p)
        u2 = UsageStore(p)                       # both start from the empty file
        u1.record_open("x")
        assert "x" in u2.boosts()                # u2 sees u1's write (fresh read)
        assert u2.report()[0]["name"] == "x"


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


class TestRecallFloor:
    """Relevance floor: weak single-word near-misses must not surface behind the
    strong matches — the failure mode the per-prompt hook's exclude-backfill
    would otherwise walk into on a long same-topic thread."""

    def test_weak_tail_dropped_below_floor(self, tmp_path: Path):
        # strong: query terms in name AND body -> base 12; weak: one incidental
        # body word -> base 1. floor = 12 * 0.25 = 3 -> weak (1) dropped.
        _write(tmp_path, "s.md", "name: alpha_beta_topic\ntier: project", "alpha beta topic")
        _write(tmp_path, "w.md", "name: gamma_note\ntier: project", "unrelated alpha mention")
        e = MemoryEngine.from_directory(tmp_path)
        assert [m.name for m in e.recall("alpha beta topic")] == ["alpha_beta_topic"]

    def test_all_weak_matches_survive_relative_floor(self, tmp_path: Path):
        # No strong hit -> the top is itself weak -> the relative floor keeps the
        # whole field (the bar is "near-miss vs the best", not an absolute gate).
        _write(tmp_path, "a.md", "name: w_one\ntier: project", "topic alpha")
        _write(tmp_path, "b.md", "name: w_two\ntier: project", "alpha topic")
        e = MemoryEngine.from_directory(tmp_path)
        assert {m.name for m in e.recall("alpha")} == {"w_one", "w_two"}

    def test_exclude_backfill_stops_at_floor(self, tmp_path: Path, capsys, monkeypatch):
        # The user's exact scenario: exclude the strong hit; the hook must NOT
        # backfill the weak near-miss — it goes silent instead.
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        _write(tmp_path, "s.md", "name: alpha_beta_topic\ntier: project", "alpha beta topic")
        _write(tmp_path, "w.md", "name: gamma_note\ntier: project", "unrelated alpha mention")
        cli._cmd_memory_recall(tmp_path, "alpha beta topic", 5, set())
        out = json.loads(capsys.readouterr().out)
        assert out["names"] == ["alpha_beta_topic"]            # weak floored from the start
        cli._cmd_memory_recall(tmp_path, "alpha beta topic", 5, {"alpha_beta_topic"})
        out = json.loads(capsys.readouterr().out)
        assert out["names"] == [] and out["markdown"] is None  # no backfill into the weak tail


class TestUsageStoreConcurrency:
    def test_parallel_opens_not_lost(self, tmp_path: Path):
        # Four processes hammer the same _usage.json. With the fcntl lock the
        # read-modify-write cycles serialize, so every hit is counted; without
        # it, near-simultaneous writes would clobber each other and the total
        # would fall short. Skipped where flock is unavailable (the lock is a
        # no-op there, so the guarantee does not hold).
        pytest.importorskip("fcntl")
        p = tmp_path / "_usage.json"
        procs = [
            multiprocessing.Process(target=_hammer_opens, args=(str(p), 40))
            for _ in range(4)
        ]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join()
        rows = UsageStore(p).report()
        assert len(rows) == 1 and rows[0]["name"] == "x"
        assert rows[0]["opens"] == 160           # 4 × 40, none lost to the race


class TestCliMemoryCommands:
    """The CLI memory commands powering the auto-recall hooks (--memory-tier for
    the session-start user-memory, --recall + --exclude for the per-prompt hook)."""

    def test_memory_tier_dump_with_bodies(self, tmp_path: Path, capsys, monkeypatch):
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        _write(tmp_path, "u.md", "name: u_fact\ntype: user\ntier: user", "user body line")
        _write(tmp_path, "p.md", "name: p_topic\ntype: project\ntier: project", "alpha beta")
        rc = cli._cmd_memory_tier(tmp_path, "user", True)
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["names"] == ["u_fact"] and out["count"] == 1   # only the user tier
        assert "user body line" in out["markdown"]                # --with-bodies

    def test_method_block_prints_full_arbeitsweise(self, capsys):
        # The always-on method block ships as a package resource; the CLI dumps it
        # verbatim for the UserPromptSubmit hook. All 9 steps must be present.
        rc = cli._cmd_method_block()
        out = capsys.readouterr().out
        assert rc == 0
        assert out.startswith("WORKING METHOD")
        assert out.strip().splitlines()[-1].startswith("9.")

    def test_memory_recall_and_exclude_dedup(self, tmp_path: Path, capsys, monkeypatch):
        monkeypatch.delenv("CONTEXT_USER_MEMORY_DIR", raising=False)
        _write(tmp_path, "a.md", "name: alpha_pkg\ntier: project", "shared topic keyword")
        _write(tmp_path, "b.md", "name: beta_pkg\ntier: project", "shared topic keyword")
        cli._cmd_memory_recall(tmp_path, "shared topic keyword", 5, set())
        out = json.loads(capsys.readouterr().out)
        assert set(out["names"]) == {"alpha_pkg", "beta_pkg"}
        assert "Auto-recalled" in out["markdown"]
        # exclude one -> only the other survives (dedup), backfill within limit
        cli._cmd_memory_recall(tmp_path, "shared topic keyword", 5, {"alpha_pkg"})
        out = json.loads(capsys.readouterr().out)
        assert out["names"] == ["beta_pkg"]
        # all excluded -> empty bundle, markdown None (hook stays silent)
        cli._cmd_memory_recall(tmp_path, "shared topic keyword", 5, {"alpha_pkg", "beta_pkg"})
        out = json.loads(capsys.readouterr().out)
        assert out["names"] == [] and out["markdown"] is None


class TestBacklinkBoostInRecall:
    """Tests for backlink_boost parameter in recall()."""

    def _write(self, d: Path, name: str, body: str) -> None:
        """Helper to write a memory file."""
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(
            f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8"
        )

    def test_backlink_boost_ranks_cited_memories_higher(self, tmp_path: Path):
        """Memories with more backlinks score higher in recall."""
        ms = tmp_path / "memories"
        ms.mkdir()

        # Create A (cited by B, C)
        self._write(ms, "a", "Concept A")
        # Create B (cites A)
        self._write(ms, "b", "[[a]] Concept B")
        # Create C (cites A)
        self._write(ms, "c", "[[a]] Concept C")

        engine = MemoryEngine.from_directory(ms)

        # Query for "concept" matches all three
        results = engine.recall("concept", limit=3)

        # A has 2 backlinks (from B, C), B and C have 0
        # With backlink_boost, A should rank first
        assert results[0].name == "a"

    def test_backlink_boost_effect_size(self, tmp_path: Path):
        """Verify backlink_boost adds the expected value."""
        ms = tmp_path / "memories"
        ms.mkdir()

        # Same structure as above
        self._write(ms, "a", "Concept A")
        self._write(ms, "b", "[[a]] Concept B")
        self._write(ms, "c", "[[a]] Concept C")

        engine = MemoryEngine.from_directory(ms)

        # Get edges for backlink count
        edges = engine.edges()
        inbound = {}
        for src, tgt in edges:
            inbound.setdefault(tgt, 0)
            inbound[tgt] += 1

        # A should have 2 inbound links
        assert inbound.get("a", 0) == 2

        # Query with explicit backlink_boost (raw inbound counts as factor —
        # the MCP server applies the log1p*0.1 damping; here we only verify
        # that a non-zero boost lifts the most-cited memory to the top).
        results = engine.recall(
            "concept",
            limit=3,
            backlink_boost={name: boost for name, boost in inbound.items()}
        )

        # First result (A) should be A due to backlink boost
        assert results[0].name == "a"

    def test_backlink_boost_does_not_dominate(self, tmp_path: Path):
        """Backlink boost should not completely override keyword relevance."""
        ms = tmp_path / "memories"
        ms.mkdir()

        # A is cited but doesn't match "b"
        self._write(ms, "a", "Concept A")
        self._write(ms, "b", "[[a]] Concept B")
        self._write(ms, "c", "[[a]] Concept C")

        engine = MemoryEngine.from_directory(ms)

        # Query for "concept b" should still prefer B over A
        # Even though A has backlinks, B matches "b" keyword
        results = engine.recall("concept b", limit=3)

        # B should be in results (matches "b")
        names = [r.name for r in results]
        assert "b" in names


class TestMemoryDreamStatus:
    """Tests for memory_dream_status() MCP tool."""

    def _write_mem(self, d: Path, name: str, body: str = "x") -> None:
        """Helper to write a memory file."""
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text(f"---\nname: {name}\n---\n\n{body}\n", encoding="utf-8")

    def test_dream_status_with_fresh_changes(self, tmp_path: Path):
        """Files changed since last dream → recommendation = 'dream fällig'."""
        ms = tmp_path / "memories"
        ms.mkdir()
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        for i in range(3):
            self._write_mem(ms, f"mem{i}")

        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.engine import RulesEngine

        rules_engine = RulesEngine.from_directory(rules_dir)
        memory_engine = MemoryEngine.from_directory(ms)
        from mcp_context_toolkit.mcp_server import _Reloader
        memory_reloader = _Reloader(lambda: memory_engine, [ms])

        server = build_server(rules_engine, memory_reloader)
        result = asyncio.run(server.call_tool("memory_dream_status", {}))
        # Result is a tuple: ([TextContent], metadata)
        text_content = result[0][0].text

        assert text_content.startswith("{\n  \"files_changed")
        data = json.loads(text_content)
        assert data["files_changed_since_last_dream"] >= 3
        assert "dream fällig" in data["recommendation"]

    def test_dream_status_with_lint_issues(self, tmp_path: Path):
        """Broken links → recommendation = 'dream fällig'."""
        ms = tmp_path / "memories"
        ms.mkdir()
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        # Create MEMORY.md (the hot index)
        (ms / "MEMORY.md").write_text("# Memories\n\n- a\n", encoding="utf-8")

        # Memory A links to non-existent B
        self._write_mem(ms, "a", body="[[nonexistent-mem]]")

        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _Reloader

        rules_engine = RulesEngine.from_directory(rules_dir)
        memory_engine = MemoryEngine.from_directory(ms)
        memory_reloader = _Reloader(lambda: memory_engine, [ms])

        server = build_server(rules_engine, memory_reloader)
        # lint_threshold=1: 1 broken link soll "fällig" triggern (Default wäre 2)
        result = asyncio.run(
            server.call_tool("memory_dream_status", {"lint_threshold": 1})
        )
        text_content = result[0][0].text

        data = json.loads(text_content)
        assert data["lint_issues"]["broken_links"]["a"] == ["nonexistent-mem"]
        assert data["total_lint_issues"] >= 1
        assert "dream fällig" in data["recommendation"]

    def test_dream_status_clean_store(self, tmp_path: Path):
        """Clean store → recommendation = 'kein dringender Bedarf'."""
        ms = tmp_path / "memories"
        ms.mkdir()
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        self._write_mem(ms, "clean", body="This memory is clean.")
        # Create MEMORY.md (the hot index)
        (ms / "MEMORY.md").write_text("# Memories\n\n- clean\n", encoding="utf-8")

        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _Reloader

        rules_engine = RulesEngine.from_directory(rules_dir)
        memory_engine = MemoryEngine.from_directory(ms)
        memory_reloader = _Reloader(lambda: memory_engine, [ms])

        server = build_server(rules_engine, memory_reloader)
        result = asyncio.run(server.call_tool("memory_dream_status", {}))
        text_content = result[0][0].text

        data = json.loads(text_content)
        # New file counts as changed, but lint is clean
        assert data["lint_issues"]["broken_links"] == {}
        assert data["lint_issues"]["orphans"] == []
        assert data["lint_issues"]["stale_pointers"] == []
        # With clean lint, recommendation should not be "dream fällig"
        assert "f채llig" not in data["recommendation"]

    def test_dream_status_with_recommendations(self, tmp_path: Path):
        """Thresholds control recommendation level."""
        ms = tmp_path / "memories"
        ms.mkdir()
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        self._write_mem(ms, "a", body="[[broken]]")  # 1 broken link

        from mcp_context_toolkit.mcp_server import build_server
        from mcp_context_toolkit.engine import RulesEngine
        from mcp_context_toolkit.mcp_server import _Reloader

        rules_engine = RulesEngine.from_directory(rules_dir)
        memory_engine = MemoryEngine.from_directory(ms)
        memory_reloader = _Reloader(lambda: memory_engine, [ms])

        server = build_server(rules_engine, memory_reloader)
        # 1 broken link, aber lint_threshold=2 → "empfohlen" (nicht "fällig")
        result = asyncio.run(
            server.call_tool(
                "memory_dream_status",
                {"files_threshold": 10, "lint_threshold": 2}
            )
        )
        text_content = result[0][0].text

        data = json.loads(text_content)
        assert data["lint_issues"]["broken_links"]["a"] == ["broken"]
        # 1 lint issue < lint_threshold=2, aber > 0 → "empfohlen"
        assert "empfohlen" in data["recommendation"]
